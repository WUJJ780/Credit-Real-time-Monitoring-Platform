"""Agent基类：模板方法 + 三层兜底 + Function Calling工具调用 + 结构化输出。

每个Agent的执行流程：
  1. collect_evidence  —— 调用数据层/工具采集证据（确定性）
  2. 决策（三层兜底）  —— LLM推理 -> 规则引擎 -> 默认兜底（保守转人工）
  3. 审计留痕 + 消息总线发布结果事件

Agent间输出统一为Pydantic结构化模型（models/risk.py），
保证可校验、可审计、可被后续Agent确定性消费。

高并发异步模式：run()为同步路径（CLI/单元测试），arun()为异步路径
（高并发流水线）。异步路径中：
  - LLM层原生异步（ainvoke + 并发信号量限流）；
  - 证据采集/规则引擎等阻塞调用经 run_blocking 卸载到受控线程池，
    事件循环始终不被阻塞。
"""
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar, Dict, List, Tuple, Type

from credit_risk_system.agents.resume.prompts import get_agent_prompt
from credit_risk_system.core.async_utils import run_blocking
from credit_risk_system.core.exceptions import LLMUnavailableError
from credit_risk_system.core.llm_factory import get_llm, get_llm_semaphore
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.message_bus import EventEnvelope, get_message_bus
from credit_risk_system.core.retry import arun_with_fallback, run_with_fallback
from credit_risk_system.core.tools import aexecute_tool, execute_tool, get_tool_schemas
from credit_risk_system.models.risk import AgentResult, PipelineContext

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
except ImportError:  # langchain-core未安装时LLM层不可用，规则层兜底
    AIMessage = HumanMessage = SystemMessage = ToolMessage = None


class BaseAgent(ABC):
    """风控Agent基类。"""

    name: ClassVar[str] = "base_agent"          # 英文名（主题/日志）
    role: ClassVar[str] = "风控岗"               # 对应风控岗位
    result_model: ClassVar[Type[AgentResult]] = AgentResult
    tool_names: ClassVar[List[str]] = []         # LLM可调用的工具

    def __init__(self):
        self.logger = get_logger(self.name)

    # ----------------------------------------------------------
    # 主流程（模板方法）
    # ----------------------------------------------------------
    def run(self, ctx: PipelineContext) -> AgentResult:
        t0 = time.perf_counter()
        self.logger.info("开始执行 | 申请单=%s | 岗位=%s",
                         ctx.application.application_id, self.role)

        # 1. 证据采集（数据分发：中台已注入基础数据，此处按岗位补充）
        evidence = self.collect_evidence(ctx)
        ctx.evidence[self.name] = evidence

        # 2. 三层兜底决策
        outcome = run_with_fallback(
            [
                ("LLM推理", lambda: self.llm_decide(ctx, evidence)),
                ("规则引擎", lambda: self.rule_decide(ctx, evidence)),
                ("默认兜底", lambda: self.default_decide(ctx, evidence)),
            ],
            context=f"{self.name}({self.role})",
            logger=self.logger,
        )
        return self._finalize_result(ctx, outcome, t0)

    async def arun(self, ctx: PipelineContext) -> AgentResult:
        """异步执行（高并发流水线路径）。

        - 证据采集/规则层：阻塞SDK调用经 run_blocking 卸载线程池；
        - LLM层：原生异步 ainvoke，受 LLM_MAX_CONCURRENCY 信号量限流。
        """
        t0 = time.perf_counter()
        self.logger.info("异步执行 | 申请单=%s | 岗位=%s",
                         ctx.application.application_id, self.role)

        # 1. 证据采集（线程池卸载，数据层内部已做连接池/锁的并发保护）
        evidence = await run_blocking(self.collect_evidence, ctx)
        ctx.evidence[self.name] = evidence

        # 2. 三层兜底决策（异步版）
        outcome = await arun_with_fallback(
            [
                ("LLM推理", lambda: self.allm_decide(ctx, evidence)),
                ("规则引擎", lambda: run_blocking(self.rule_decide, ctx, evidence)),
                ("默认兜底", lambda: run_blocking(self.default_decide, ctx, evidence)),
            ],
            context=f"{self.name}({self.role})",
            logger=self.logger,
        )
        return await self._afinalize_result(ctx, outcome, t0)

    # ----------------------------------------------------------
    # 结果标准化 + 审计 + 事件发布（同步/异步共用）
    # ----------------------------------------------------------
    def _finalize_result(self, ctx: PipelineContext, outcome, t0: float) -> AgentResult:
        result = self._standardize(ctx, outcome, t0)
        self._publish(ctx, result)
        return result

    async def _afinalize_result(self, ctx: PipelineContext, outcome, t0: float) -> AgentResult:
        result = self._standardize(ctx, outcome, t0)
        await self._apublish(ctx, result)
        return result

    def _standardize(self, ctx: PipelineContext, outcome, t0: float) -> AgentResult:
        result = self._coerce(outcome.value)
        result.agent = self.name
        result.layer = outcome.layer
        result.executed_at = datetime.now()
        result.elapsed_ms = int((time.perf_counter() - t0) * 1000)

        ctx.record_event(
            self.name, outcome.layer,
            f"{self.role}结论: {result.status.value} | {result.summary}",
            level="INFO" if result.status.value == "PASS" else "WARNING",
        )
        self.logger.info("执行完成 | 结论=%s | 决策层=%s | 耗时=%dms",
                         result.status.value, outcome.layer, result.elapsed_ms)
        return result

    def _publish(self, ctx: PipelineContext, result: AgentResult) -> None:
        try:
            get_message_bus().publish(EventEnvelope(
                topic=f"risk.{self.name}",
                source=self.name,
                application_id=ctx.application.application_id,
                event_type=result.status.value,
                payload=result.model_dump(mode="json"),
            ))
        except Exception as e:
            self.logger.warning("结果事件发布失败: %s", e)

    async def _apublish(self, ctx: PipelineContext, result: AgentResult) -> None:
        try:
            await get_message_bus().apublish(EventEnvelope(
                topic=f"risk.{self.name}",
                source=self.name,
                application_id=ctx.application.application_id,
                event_type=result.status.value,
                payload=result.model_dump(mode="json"),
            ))
        except Exception as e:
            self.logger.warning("结果事件发布失败: %s", e)

    # ----------------------------------------------------------
    # 三层兜底: LLM推理
    # ----------------------------------------------------------
    def llm_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> AgentResult:
        """LLM结构化推理（Function Calling）。失败时向上抛出触发降级。"""
        llm = get_llm()
        if llm is None or SystemMessage is None:
            raise LLMUnavailableError("LLM未配置或langchain-core未安装")

        system_prompt, user_prompt = self.build_prompt(ctx, evidence)
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

        # 可选: 工具调用阶段（Function Calling + MCP）
        messages = self._maybe_run_tools(llm, messages)

        structured = llm.with_structured_output(self.result_model)
        return structured.invoke(messages)

    async def allm_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> AgentResult:
        """异步LLM结构化推理：ainvoke + LLM并发信号量限流。"""
        llm = get_llm()
        if llm is None or SystemMessage is None:
            raise LLMUnavailableError("LLM未配置或langchain-core未安装")

        system_prompt, user_prompt = self.build_prompt(ctx, evidence)
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

        sem = get_llm_semaphore()
        if sem is None:  # 非事件循环环境（不应发生），退化为同步调用
            return self.llm_decide(ctx, evidence)

        async with sem:  # 整个LLM阶段（工具调用+结构化推理）持有限流信号量
            messages = await self._amaybe_run_tools(llm, messages)
            structured = llm.with_structured_output(self.result_model)
            return await structured.ainvoke(messages)

    def _maybe_run_tools(self, llm, messages: list) -> list:
        """允许LLM先调用工具采集数据，再进行结构化决策。"""
        if not self.tool_names:
            return messages
        try:
            tool_schemas = get_tool_schemas(self.tool_names)
            resp = llm.bind_tools(tool_schemas).invoke(messages)
            tool_calls = getattr(resp, "tool_calls", None) or []
            if not tool_calls:
                return messages
            messages.append(AIMessage(content=resp.content or "", tool_calls=tool_calls))
            for tc in tool_calls:
                self.logger.debug("LLM调用工具: %s(%s)", tc["name"], tc["args"])
                output = execute_tool(tc["name"], tc.get("args") or {})
                messages.append(ToolMessage(content=output, tool_call_id=tc["id"]))
        except Exception as e:
            self.logger.warning("工具调用阶段异常(%s)，直接进入结构化推理", e)
        return messages

    async def _amaybe_run_tools(self, llm, messages: list) -> list:
        """异步工具调用阶段：ainvoke + 工具经受控线程池执行。"""
        if not self.tool_names:
            return messages
        try:
            tool_schemas = get_tool_schemas(self.tool_names)
            resp = await llm.bind_tools(tool_schemas).ainvoke(messages)
            tool_calls = getattr(resp, "tool_calls", None) or []
            if not tool_calls:
                return messages
            messages.append(AIMessage(content=resp.content or "", tool_calls=tool_calls))
            for tc in tool_calls:
                self.logger.debug("LLM调用工具: %s(%s)", tc["name"], tc["args"])
                output = await aexecute_tool(tc["name"], tc.get("args") or {})
                messages.append(ToolMessage(content=output, tool_call_id=tc["id"]))
        except Exception as e:
            self.logger.warning("工具调用阶段异常(%s)，直接进入结构化推理", e)
        return messages

    # ----------------------------------------------------------
    # 抽象接口（子类实现）
    # ----------------------------------------------------------
    @abstractmethod
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        """采集本岗位所需证据（数据分发结果）。"""

    def build_prompt(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> Tuple[str, str]:
        """构造LLM提示词，返回(system_prompt, user_prompt)。

        默认从提示词库 resume/prompts.py 按 self.name 取本Agent的提示词套装
        （系统提示词的决策口径引用risk_strategy配置常量，与规则引擎同步）；
        子类可覆盖以实现定制化提示词。
        """
        spec = get_agent_prompt(self.name)
        return spec.system, spec.render_user(ctx.application, evidence)

    @abstractmethod
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> AgentResult:
        """规则引擎决策（确定性主决策路径，离线可用）。"""

    @abstractmethod
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> AgentResult:
        """默认兜底决策（保守策略：宁可转人工，不可误放行）。"""

    # ----------------------------------------------------------
    # 公共工具
    # ----------------------------------------------------------
    def _coerce(self, value: Any) -> AgentResult:
        if isinstance(value, self.result_model):
            return value
        if isinstance(value, dict):
            return self.result_model.model_validate(value)
        raise TypeError(f"Agent[{self.name}]输出类型异常: {type(value)}")

    @staticmethod
    def _facts(ctx: PipelineContext, evidence: Dict[str, Any], **extra) -> Dict[str, Any]:
        """合并申请单基础事实 + 证据 + 补充字段，构造标准化事实字典。"""
        facts = ctx.application.facts()
        facts.update(evidence)
        facts.update(extra)
        return facts
