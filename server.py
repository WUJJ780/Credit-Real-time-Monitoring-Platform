"""信贷风控异步API服务（FastAPI + uvicorn，高并发异步模式）。

启动方式：
    python server.py                         # 默认 0.0.0.0:8000
    uvicorn server:app --workers 4           # 多worker生产部署
    python scripts/load_test.py              # 高并发压测

接口：
    POST /api/v1/applications                 受理申请（202立即返回，后台异步处理）
    GET  /api/v1/applications/{id}            查询处理状态与最终决策
    GET  /api/v1/applications/{id}/events     SSE实时推送处理进度
    GET  /api/v1/applications                 列表最近受理的申请
    POST /api/v1/demo                         批量受理内置演示案例
    GET  /api/v1/stats                        流水线运行统计（吞吐/耗时分位）
    GET  /healthz                             健康检查
"""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from credit_risk_system.config.settings import settings
from credit_risk_system.core.async_pipeline import get_async_pipeline
from credit_risk_system.core.logger import get_logger
from credit_risk_system.models.loan import Document, LoanApplication

logger = get_logger("server")

_STATIC_DIR = Path(__file__).resolve().parent / "frontend"
_TERMINAL_EVENTS = ("completed", "failed", "timeout")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("风控异步服务启动 | 决策模式=%s | 端口=%d",
                "LLM推理+规则引擎" if settings.llm_enabled else "规则引擎（离线）",
                settings.SERVER_PORT)
    yield
    await pipeline.shutdown()


app = FastAPI(
    title="信贷自动风控多Agent系统 API",
    description="异步高并发风控流水线：受理→LangGraph异步编排→结果查询/SSE进度推送",
    version="2.0-async",
    lifespan=lifespan,
)

pipeline = get_async_pipeline()

# 问答RAGAgent（面向用户的智能助手，绑定流水线以支持补件/进度查询）
from credit_risk_system.agents.rag_agent import get_rag_agent  # noqa: E402

rag_agent = get_rag_agent(pipeline=pipeline)

# ============================================================
# 演示前端页面（静态托管，浏览器打开 http://127.0.0.1:8000）
# ============================================================
app.mount("/frontend", StaticFiles(directory=str(_STATIC_DIR)), name="frontend")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """风控演示控制台首页。"""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/submit", include_in_schema=False)
async def submit_page() -> FileResponse:
    """贷款申请单在线填写页（演示前端）。"""
    return FileResponse(_STATIC_DIR / "submit.html")


@app.get("/chat", include_in_schema=False)
async def chat_page() -> FileResponse:
    """智能助手对话页（RAG问答Agent演示前端）。"""
    return FileResponse(_STATIC_DIR / "chat.html")


# ============================================================
# 健康检查
# ============================================================
@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "mode": "llm" if settings.llm_enabled else "rule-engine"}


# ============================================================
# 申请受理与查询
# ============================================================
@app.post("/api/v1/applications", status_code=202)
async def submit_application(application: LoanApplication) -> dict:
    """受理贷款申请：立即返回202，后台asyncio.Task异步处理（幂等）。"""
    app_id = await pipeline.submit(application)
    return {
        "application_id": app_id,
        "message": "已受理，异步处理中",
        "status_url": f"/api/v1/applications/{app_id}",
        "events_url": f"/api/v1/applications/{app_id}/events",
    }


@app.post("/api/v1/applications/batch", status_code=202)
async def submit_batch(applications: List[LoanApplication]) -> dict:
    """批量受理：并发提交多个申请。"""
    app_ids = await pipeline.submit_many(applications)
    return {"accepted": len(app_ids), "application_ids": app_ids}


@app.get("/api/v1/applications/{application_id}")
async def get_application_status(application_id: str) -> dict:
    """查询申请处理状态（SUBMITTED/PROCESSING/COMPLETED/FAILED/TIMEOUT）。"""
    status = await pipeline.get_status(application_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"申请单不存在: {application_id}")
    return status


@app.get("/api/v1/applications")
async def list_applications(
    limit: int = Query(default=50, ge=1, le=1000),
) -> dict:
    """列表最近受理的申请（含状态与耗时），total为受理总数。"""
    ids = pipeline.record_ids()
    items = []
    for app_id in reversed(ids):      # 最新受理在前
        info = await pipeline.get_status(app_id)
        if info is not None:
            items.append(info)
        if len(items) >= limit:
            break
    return {"total": len(ids), "count": len(items), "applications": items}


@app.get("/api/v1/applications/{application_id}/events")
async def stream_events(application_id: str) -> StreamingResponse:
    """SSE实时推送处理进度（stage/completed/failed事件）。"""
    queue = pipeline.subscribe_events(application_id)
    if queue is None:
        raise HTTPException(status_code=404, detail=f"申请单不存在: {application_id}")

    async def gen():
        try:
            while True:
                event = await queue.get()
                payload = json.dumps(event, ensure_ascii=False, default=str)
                yield f"data: {payload}\n\n"
                if event.get("event") in _TERMINAL_EVENTS:
                    break
        except asyncio.CancelledError:
            logger.debug("SSE客户端断开: %s", application_id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ============================================================
# 智能助手（问答RAGAgent）与补件闭环
# ============================================================
class ChatRequest(BaseModel):
    """对话请求：会话内多轮（session_id保持不变即可记住上下文）。"""
    session_id: str = Field(description="会话ID（前端生成并保持）")
    message: str = Field(description="用户消息")


class SupplementRequest(BaseModel):
    """补件请求：补齐后的完整资料清单（覆盖原申请的资料）。"""
    documents: List[Document] = Field(description="补齐后的资料清单")


@app.post("/api/v1/chat")
async def chat(req: ChatRequest) -> dict:
    """智能助手对话：理解需求→检索知识库→回答/引导操作/办理补件。

    返回 {reply, action?, suggestions?, hint?}：
      action.type=navigate 时前端跳转 action.url（如申请单填写页）；
      hint 为多轮状态机提示（等待申请单号/等待补件确认）。
    """
    return await rag_agent.chat(req.session_id, req.message)


@app.get("/api/v1/chat/history/{session_id}")
async def chat_history(session_id: str) -> dict:
    """获取会话历史：前端刷新/重开页面后恢复多轮对话上下文。"""
    return {"session_id": session_id, "history": rag_agent.get_history(session_id)}


@app.post("/api/v1/applications/{application_id}/supplement", status_code=202)
async def supplement_application(application_id: str, body: SupplementRequest) -> dict:
    """补件重新受理：以补齐后的资料清单重新跑完整风控流程。

    仅当原申请为SUPPLEMENT终态时允许（其他终态直接拒绝）。
    """
    original = pipeline.get_application(application_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"申请单不存在: {application_id}")
    status_now = await pipeline.get_status(application_id)
    decision = (status_now or {}).get("final_decision", {}).get("decision")
    if decision != "SUPPLEMENT":
        raise HTTPException(status_code=400,
                            detail=f"该申请无需补件（当前决策: {decision or '处理中'}）")
    updated = original.model_copy(deep=True)
    updated.documents = body.documents
    await pipeline.resubmit(updated)
    return {
        "application_id": application_id,
        "message": "补件已受理，重新审核中",
        "status_url": f"/api/v1/applications/{application_id}",
    }


# ============================================================
# 演示与统计
# ============================================================
@app.post("/api/v1/demo", status_code=202)
async def submit_demo_cases(
    cases: Optional[str] = Query(default=None,
                                 description="逗号分隔: normal,fraud,review,supplement,compliance（默认全部）"),
    times: int = Query(default=1, ge=1, le=500,
                       description="每案例生成变体数量（>1时批量模拟高并发，变体唯一单号/设备）"),
) -> dict:
    """批量受理内置演示案例（同一案例重复提交幂等返回已受理）。

    times>1 时以模板生成独立变体（保留三要素核验身份、唯一设备/IP），
    用于前端「一键模拟高并发」：大量同手机号申请会真实触发图谱
    SHARED_PHONE 团伙关联检测，决策保守转人工复核。
    """
    from main import build_demo_cases

    all_cases = build_demo_cases()
    if cases is None or "all" in [c.strip() for c in cases.split(",")]:
        selected = list(all_cases)
    else:
        selected = [c.strip() for c in cases.split(",") if c.strip()]
    unknown = [c for c in selected if c not in all_cases]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知案例: {unknown}")
    if times == 1:
        app_ids = await pipeline.submit_many([all_cases[c] for c in selected])
        return {"accepted": len(app_ids), "cases": selected, "application_ids": app_ids}

    batch_id = uuid4().hex[:6]
    batch = []
    for i in range(times):
        for c in selected:
            batch.append(_variant(all_cases[c], f"{batch_id}-{i:04d}"))
    app_ids = await pipeline.submit_many(batch)
    return {"accepted": len(app_ids), "cases": selected, "times": times,
            "application_ids": app_ids}


def _variant(template: LoanApplication, uid: str) -> LoanApplication:
    """基于演示案例模板生成独立申请单（唯一单号/申请人/设备/IP）。"""
    app = template.model_copy(deep=True)
    app.application_id = f"{template.application_id}-V{uid}"
    app.applicant.applicant_id = f"V-CUST-{uid}"
    app.device.device_id = f"V-DEV-{uid}"
    app.device.fingerprint = f"v-fp-{uid}"
    n = sum(ord(ch) for ch in uid) % 256
    app.device.ip = f"10.{n % 250 + 1}.{n % 256}.{n % 256}"
    return app


@app.get("/api/v1/stats")
async def stats() -> dict:
    """流水线运行统计：吞吐、耗时分位、在途/排队数。"""
    return await pipeline.stats()


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        log_level="info",
    )
