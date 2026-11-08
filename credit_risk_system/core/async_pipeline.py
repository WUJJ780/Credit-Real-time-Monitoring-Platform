"""异步高并发风控流水线：受理 -> 并发处理 -> 进度/事件流 -> 结果查询。

核心能力：
  1. submit()  受理申请，立即返回，后台asyncio.Task异步处理（幂等：重复提交返回已受理）；
  2. 并发控制  ASYNC_MAX_APPLICATIONS 信号量限制同时处理的申请数，超限排队；
  3. 超时熔断  单申请流程超过 PIPELINE_TIMEOUT_SECONDS 标记TIMEOUT；
  4. 进度/事件 每完成一个环节更新stage，事件通过per-application队列推送（SSE消费）；
  5. 统计指标  提交/完成/失败/超时计数、平均与P99耗时（供监控与压测）。

典型用法：
    pipeline = AsyncRiskPipeline()
    pipeline.submit(application)                 # 不等待
    status = await pipeline.get_status(app_id)   # 轮询进度
    final  = await pipeline.wait(app_id)         # 等待最终决策
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from credit_risk_system.agents.resume.state import (
    ApplicationStatus,
    FinalDecision,
    PipelineContext,
    PipelineEvent,
    PipelineStats,
    StatusPayload,
)
from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.orchestrator import get_orchestrator
from credit_risk_system.models.loan import LoanApplication

logger = get_logger("async_pipeline")

# 结果跟踪上限：超过后清理最旧的已完成记录，防止无限增长
_MAX_TRACKED = 10_000


@dataclass
class _Record:
    application: LoanApplication
    status: ApplicationStatus = ApplicationStatus.SUBMITTED
    stage: str = "START"
    started_at: float = field(default_factory=time.perf_counter)
    finished_at: Optional[float] = None
    elapsed_ms: int = 0
    final_decision: Optional[FinalDecision] = None
    audit_events: List[dict] = field(default_factory=list)
    error: str = ""
    events: "asyncio.Queue[dict]" = field(default_factory=lambda: asyncio.Queue(maxsize=500))


class AsyncRiskPipeline:
    """异步高并发风控流水线。"""

    def __init__(self):
        self._records: Dict[str, _Record] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # 并发上限信号量：同时处理的申请数不超过 ASYNC_MAX_APPLICATIONS
        self._sem = asyncio.Semaphore(max(1, settings.ASYNC_MAX_APPLICATIONS))
        self._stats = PipelineStats()
        self._lock = asyncio.Lock()
        logger.info("异步流水线初始化完成 | 并发上限=%d | 单笔超时=%ds | LLM并发=%d",
                    settings.ASYNC_MAX_APPLICATIONS,
                    settings.PIPELINE_TIMEOUT_SECONDS,
                    settings.LLM_MAX_CONCURRENCY)

    # ----------------------------------------------------------
    # 受理
    # ----------------------------------------------------------
    async def submit(self, application: LoanApplication) -> str:
        """受理申请（幂等）。已受理的申请单号返回原单，不重复建任务。"""
        app_id = application.application_id
        async with self._lock:
            existing = self._records.get(app_id)
            if existing is not None and existing.status in (
                    ApplicationStatus.SUBMITTED, ApplicationStatus.PROCESSING):
                existing.status = ApplicationStatus.DUPLICATE  # 标记重复受理（进度不变）
                return app_id
            if existing is not None and existing.status is ApplicationStatus.DUPLICATE:
                return app_id
            record = _Record(application=application)
            self._records[app_id] = record
            self._stats.submitted += 1
        task = asyncio.create_task(
            self._process(app_id), name=f"risk-pipeline-{app_id}")
        self._tasks[app_id] = task
        await self._emit(app_id, {"event": "submitted", "application_id": app_id})
        logger.info("申请单已受理: %s（当前在途 %d）", app_id, self._stats.submitted)
        return app_id

    async def submit_many(self, applications: List[LoanApplication]) -> List[str]:
        """批量受理（并发提交）。"""
        return [await self.submit(app) for app in applications]

    async def resubmit(self, application: LoanApplication) -> str:
        """补件后重新受理：以原申请单号强制开启新一轮处理（覆盖旧终态记录）。

        用于补件闭环——原申请SUPPLEMENT终态后，用户补齐资料，
        由RAGAgent调用本方法重新跑完整流程（原审计记录在MinIO/output留痕中保留）。
        """
        app_id = application.application_id
        async with self._lock:
            record = _Record(application=application)
            self._records[app_id] = record
            self._stats.submitted += 1
        task = asyncio.create_task(
            self._process(app_id), name=f"risk-pipeline-resubmit-{app_id}")
        self._tasks[app_id] = task
        await self._emit(app_id, {"event": "submitted", "application_id": app_id})
        logger.info("申请单重新受理（补件）: %s", app_id)
        return app_id

    def get_application(self, application_id: str) -> Optional[LoanApplication]:
        """获取已受理申请单的原始数据（供补件/查询使用）。"""
        record = self._records.get(application_id)
        return record.application if record is not None else None

    # ----------------------------------------------------------
    # 处理
    # ----------------------------------------------------------
    async def _process(self, app_id: str) -> Optional[FinalDecision]:
        record = self._records[app_id]
        try:
            async with self._sem:  # 全局并发上限，超限在此排队
                await self._set(record, ApplicationStatus.PROCESSING, stage="dispatch")
                await self._emit(app_id, {"event": "processing"})
                last_ctx = await self._run_graph(record.application, app_id)
                final = last_ctx.final_decision
                await self._set(record, ApplicationStatus.COMPLETED,
                                stage="finalize", final_decision=final,
                                audit_events=[e.model_dump(mode="json")
                                              for e in last_ctx.audit_events])
                await self._emit(app_id, {
                    "event": "completed",
                    "decision": final.decision.value,
                    "final_decision": final.model_dump(mode="json"),
                })
                logger.info("申请单处理完成: %s | 决策=%s | 耗时=%dms",
                            app_id, final.decision.value, record.elapsed_ms)
                return final
        except asyncio.TimeoutError:
            await self._set(record, ApplicationStatus.TIMEOUT, error="流程超时")
            await self._emit(app_id, {"event": "timeout"})
            logger.warning("申请单处理超时: %s（>%ds）",
                           app_id, settings.PIPELINE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._set(record, ApplicationStatus.FAILED, error=str(e))
            await self._emit(app_id, {"event": "failed", "error": str(e)})
            logger.exception("申请单处理异常: %s", app_id)
        finally:
            self._tasks.pop(app_id, None)
            self._maybe_evict()

    async def _run_graph(self, application: LoanApplication, app_id: str) -> PipelineContext:
        """驱动异步图执行，逐节点更新进度并推送事件。"""
        orchestrator = get_orchestrator()
        last_ctx: Optional[PipelineContext] = None

        async def _drive():
            nonlocal last_ctx
            async for node, ctx in orchestrator.astream(application):
                last_ctx = ctx
                await self._set(self._records[app_id], None, stage=node)
                await self._emit(app_id, {"event": "stage", "stage": node})

        # 单笔超时熔断（wait_for会取消内部任务，LangGraph异步图支持取消）
        await asyncio.wait_for(_drive(), timeout=settings.PIPELINE_TIMEOUT_SECONDS)
        assert last_ctx is not None, "流程未产生任何节点输出"
        return last_ctx

    # ----------------------------------------------------------
    # 查询
    # ----------------------------------------------------------
    async def get_status(self, application_id: str) -> Optional[StatusPayload]:
        """查询申请处理状态（进度+最终决策），载荷结构见StatusPayload。"""
        async with self._lock:
            record = self._records.get(application_id)
            if record is None:
                return None
            info: StatusPayload = {
                "application_id": application_id,
                "status": record.status.value,
                "stage": record.stage,
                "elapsed_ms": record.elapsed_ms,
                "error": record.error,
            }
            if record.final_decision is not None:
                info["final_decision"] = record.final_decision.model_dump(mode="json")
            if record.audit_events:
                info["audit_events"] = list(record.audit_events)
            return info

    async def wait(self, application_id: str, timeout: Optional[float] = None) -> Optional[FinalDecision]:
        """等待申请处理完成，返回最终决策（未受理返回None）。"""
        task = self._tasks.get(application_id)
        if task is None:
            record = self._records.get(application_id)
            return record.final_decision if record else None
        try:
            return await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            record = self._records.get(application_id)
            return record.final_decision if record else None

    def subscribe_events(self, application_id: str) -> Optional["asyncio.Queue[dict]"]:
        """订阅申请处理事件流（SSE等实时推送消费）。"""
        record = self._records.get(application_id)
        return record.events if record is not None else None

    def record_ids(self) -> List[str]:
        """已受理申请单号列表（按受理顺序）。"""
        return list(self._records.keys())

    async def stats(self) -> dict:
        """流水线运行统计。"""
        async with self._lock:
            return self._stats.to_dict()

    # ----------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------
    async def shutdown(self) -> None:
        """关闭流水线：取消全部在途任务（服务停机时调用）。"""
        async with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("异步流水线已关闭（取消在途任务 %d 个）", len(tasks))

    # ----------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------
    async def _set(self, record: _Record, status: Optional[ApplicationStatus],
                   *, stage: Optional[str] = None,
                   final_decision: Optional[FinalDecision] = None,
                   audit_events: Optional[List[dict]] = None,
                   error: str = "") -> None:
        async with self._lock:
            if status is not None:
                record.status = status
            if stage is not None:
                record.stage = stage
            if final_decision is not None:
                record.final_decision = final_decision
            if audit_events is not None:
                record.audit_events = list(audit_events)
            if error:
                record.error = error
            if status in (ApplicationStatus.COMPLETED, ApplicationStatus.FAILED,
                          ApplicationStatus.TIMEOUT):
                record.finished_at = time.perf_counter()
                record.elapsed_ms = int((record.finished_at - record.started_at) * 1000)
                if status is ApplicationStatus.COMPLETED:
                    self._stats.completed += 1
                    lat = record.elapsed_ms
                    self._stats.total_elapsed_ms += lat
                    self._stats.latencies_ms.append(lat)
                    self._stats.min_elapsed_ms = lat if not self._stats.min_elapsed_ms \
                        else min(self._stats.min_elapsed_ms, lat)
                    self._stats.max_elapsed_ms = max(self._stats.max_elapsed_ms, lat)
                elif status is ApplicationStatus.FAILED:
                    self._stats.failed += 1
                else:
                    self._stats.timed_out += 1
            self._refresh_stats_counts()

    def _refresh_stats_counts(self) -> None:
        in_flight = sum(1 for r in self._records.values()
                        if r.status is ApplicationStatus.PROCESSING)
        queued = sum(1 for r in self._records.values()
                     if r.status is ApplicationStatus.SUBMITTED)
        self._stats.in_flight = in_flight
        self._stats.queued = queued

    async def _emit(self, app_id: str, event: PipelineEvent) -> None:
        """推送事件到该申请的事件队列（SSE消费），载荷结构见PipelineEvent。"""
        record = self._records.get(app_id)
        if record is None:
            return
        event.setdefault("ts", time.time())
        try:
            record.events.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 消费过慢时丢弃事件，避免阻塞流水线

    def _maybe_evict(self) -> None:
        """超过跟踪上限后清理最旧的终态记录。"""
        if len(self._records) <= _MAX_TRACKED:
            return
        finished = [app_id for app_id, r in self._records.items()
                    if r.status in (ApplicationStatus.COMPLETED, ApplicationStatus.FAILED,
                                    ApplicationStatus.TIMEOUT)]
        for app_id in finished[: len(self._records) - _MAX_TRACKED]:
            del self._records[app_id]


_pipeline: Optional[AsyncRiskPipeline] = None


def get_async_pipeline() -> AsyncRiskPipeline:
    """获取异步流水线单例（进程内共享，供API服务与压测脚本使用）。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = AsyncRiskPipeline()
    return _pipeline
