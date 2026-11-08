"""Agent编排器：基于LangGraph的信贷风控流程编排。

流程拓扑：
  START -> dispatch(风控中台受理/数据分发)
        -> intake(进件审核)
              ├─ PASS       -> antifraud(反欺诈)
              │                 ├─ PASS -> credit(信用评估) -> approval(审批决策)
              │                 └─ REJECT/其他 -> compliance(合规审计) -> finalize
              └─ REJECT/SUPPLEMENT -> compliance -> finalize
        -> compliance(合规审计，全流程检查)
              ├─ 有审批且可放行 -> postloan(贷后管理) -> finalize
              └─ 其他 -> finalize(决策总控)
        -> END

以申请单号作为thread_id，使用MemorySaver检查点支持中断恢复。

高并发异步模式：同一拓扑编译为「同步图 + 异步图」两张图。
  - 同步图 graph  : 节点为同步函数，供CLI演示/单元测试（invoke/stream）；
  - 异步图 agraph : 节点为协程，供高并发流水线（ainvoke/astream）。
异步图节点调用 Agent.arun()：LLM原生异步 + 阻塞I/O线程池卸载，
事件循环始终不被阻塞；条件路由逻辑两张图共用。
"""
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple

from langgraph.graph import END, START, StateGraph

from credit_risk_system.agents import (
    AntiFraudAgent,
    ApprovalAgent,
    ComplianceAgent,
    CreditAgent,
    IntakeAgent,
    MiddlePlatformAgent,
    PostLoanAgent,
)
from credit_risk_system.agents.resume.state import (
    AgentStatus,
    FinalDecision,
    PipelineContext,
    WorkflowState,
)
from credit_risk_system.core.async_utils import run_blocking
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.memory import get_checkpointer
from credit_risk_system.models.loan import LoanApplication

logger = get_logger("orchestrator")


class CreditRiskOrchestrator:
    """信贷风控流程编排器（LangGraph StateGraph）。"""

    def __init__(self):
        self.middle = MiddlePlatformAgent()
        self.intake_agent = IntakeAgent()
        self.antifraud_agent = AntiFraudAgent()
        self.credit_agent = CreditAgent()
        self.approval_agent = ApprovalAgent()
        self.compliance_agent = ComplianceAgent()
        self.postloan_agent = PostLoanAgent()
        self.graph = self._build_graph()
        self.agraph = self._build_agraph()
        logger.info("风控流程编排图构建完成（同步图 + 异步图）")

    # ----------------------------------------------------------
    # 图构建
    # ----------------------------------------------------------
    def _build_graph(self):
        g = StateGraph(WorkflowState)

        g.add_node("dispatch", self._node("dispatch", self.middle.dispatch))
        g.add_node("intake", self._node("intake", self._run_intake))
        g.add_node("antifraud", self._node("antifraud", self._run_antifraud))
        g.add_node("credit", self._node("credit", self._run_credit))
        g.add_node("approval", self._node("approval", self._run_approval))
        g.add_node("compliance", self._node("compliance", self._run_compliance))
        g.add_node("postloan", self._node("postloan", self._run_postloan))
        g.add_node("finalize", self._node("finalize", self._run_finalize))

        self._add_edges(g)
        return g.compile(checkpointer=get_checkpointer())

    def _build_agraph(self):
        """异步图：节点为协程，由ainvoke/astream驱动（高并发流水线）。"""
        g = StateGraph(WorkflowState)

        g.add_node("dispatch", self._anode("dispatch", self._adispatch))
        g.add_node("intake", self._anode("intake", self._arun_intake))
        g.add_node("antifraud", self._anode("antifraud", self._arun_antifraud))
        g.add_node("credit", self._anode("credit", self._arun_credit))
        g.add_node("approval", self._anode("approval", self._arun_approval))
        g.add_node("compliance", self._anode("compliance", self._arun_compliance))
        g.add_node("postloan", self._anode("postloan", self._arun_postloan))
        g.add_node("finalize", self._anode("finalize", self._arun_finalize))

        self._add_edges(g)
        return g.compile(checkpointer=get_checkpointer())

    @staticmethod
    def _add_edges(g: StateGraph) -> None:
        g.add_edge(START, "dispatch")
        g.add_edge("dispatch", "intake")
        g.add_conditional_edges(
            "intake", CreditRiskOrchestrator._route_after_intake,
            {"antifraud": "antifraud", "compliance": "compliance"})
        g.add_conditional_edges(
            "antifraud", CreditRiskOrchestrator._route_after_antifraud,
            {"credit": "credit", "compliance": "compliance"})
        g.add_edge("credit", "approval")
        g.add_edge("approval", "compliance")
        g.add_conditional_edges(
            "compliance", CreditRiskOrchestrator._route_after_compliance,
            {"postloan": "postloan", "finalize": "finalize"})
        g.add_edge("postloan", "finalize")
        g.add_edge("finalize", END)

    # ----------------------------------------------------------
    # 节点执行器
    # ----------------------------------------------------------
    def _node(self, stage: str, fn):
        def node(state: WorkflowState) -> Dict[str, Any]:
            ctx: PipelineContext = state["context"]
            errors = list(state.get("errors") or [])
            try:
                fn(ctx)
            except Exception as e:
                errors.append(f"{stage}: {e}")
                logger.exception("节点[%s]执行异常，记录并继续", stage)
                ctx.record_event(stage, "system",
                                 f"节点执行异常: {e}", level="VIOLATION")
            return {"context": ctx, "stage": stage, "errors": errors}
        return node

    def _anode(self, stage: str, afn):
        async def node(state: WorkflowState) -> Dict[str, Any]:
            ctx: PipelineContext = state["context"]
            errors = list(state.get("errors") or [])
            try:
                await afn(ctx)
            except Exception as e:
                errors.append(f"{stage}: {e}")
                logger.exception("异步节点[%s]执行异常，记录并继续", stage)
                ctx.record_event(stage, "system",
                                 f"节点执行异常: {e}", level="VIOLATION")
            return {"context": ctx, "stage": stage, "errors": errors}
        return node

    def _run_intake(self, ctx: PipelineContext) -> None:
        ctx.intake_result = self.intake_agent.run(ctx)

    def _run_antifraud(self, ctx: PipelineContext) -> None:
        ctx.antifraud_result = self.antifraud_agent.run(ctx)

    def _run_credit(self, ctx: PipelineContext) -> None:
        ctx.credit_result = self.credit_agent.run(ctx)

    def _run_approval(self, ctx: PipelineContext) -> None:
        ctx.approval_result = self.approval_agent.run(ctx)

    def _run_compliance(self, ctx: PipelineContext) -> None:
        ctx.compliance_result = self.compliance_agent.run(ctx)

    def _run_postloan(self, ctx: PipelineContext) -> None:
        ctx.postloan_result = self.postloan_agent.run(ctx)

    def _run_finalize(self, ctx: PipelineContext) -> None:
        self.middle.finalize(ctx)

    # ---------- 异步节点 ----------
    async def _adispatch(self, ctx: PipelineContext) -> None:
        # 数据分发含图谱写入与消息发布，卸载线程池执行
        await run_blocking(self.middle.dispatch, ctx)

    async def _arun_intake(self, ctx: PipelineContext) -> None:
        ctx.intake_result = await self.intake_agent.arun(ctx)

    async def _arun_antifraud(self, ctx: PipelineContext) -> None:
        ctx.antifraud_result = await self.antifraud_agent.arun(ctx)

    async def _arun_credit(self, ctx: PipelineContext) -> None:
        ctx.credit_result = await self.credit_agent.arun(ctx)

    async def _arun_approval(self, ctx: PipelineContext) -> None:
        ctx.approval_result = await self.approval_agent.arun(ctx)

    async def _arun_compliance(self, ctx: PipelineContext) -> None:
        ctx.compliance_result = await self.compliance_agent.arun(ctx)

    async def _arun_postloan(self, ctx: PipelineContext) -> None:
        ctx.postloan_result = await self.postloan_agent.arun(ctx)

    async def _arun_finalize(self, ctx: PipelineContext) -> None:
        # 决策总控含MySQL/MinIO持久化与消息发布，卸载线程池执行
        await run_blocking(self.middle.finalize, ctx)

    # ----------------------------------------------------------
    # 条件路由
    # ----------------------------------------------------------
    @staticmethod
    def _route_after_intake(state: WorkflowState) -> str:
        result = state["context"].intake_result
        return "antifraud" if result and result.status is AgentStatus.PASS else "compliance"

    @staticmethod
    def _route_after_antifraud(state: WorkflowState) -> str:
        result = state["context"].antifraud_result
        return "credit" if result and result.status is AgentStatus.PASS else "compliance"

    @staticmethod
    def _route_after_compliance(state: WorkflowState) -> str:
        ctx = state["context"]
        approval = ctx.approval_result
        # 合规不通过时直接进入决策总控（由中台执行一票否决）
        if ctx.compliance_result is not None and not ctx.compliance_result.passed:
            return "finalize"
        if approval is not None and approval.decision.value in ("AUTO_APPROVE", "MANUAL_REVIEW"):
            return "postloan"
        return "finalize"

    # ----------------------------------------------------------
    # 对外接口
    # ----------------------------------------------------------
    def _initial_state(self, application: LoanApplication) -> Tuple[WorkflowState, dict]:
        state: WorkflowState = {
            "application_id": application.application_id,
            "context": PipelineContext(application=application),
            "stage": "START",
            "errors": [],
        }
        config = {"configurable": {"thread_id": application.application_id}}
        return state, config

    def run(self, application: LoanApplication) -> FinalDecision:
        """同步执行完整流程，返回最终决策。"""
        state, config = self._initial_state(application)
        final_state = self.graph.invoke(state, config)
        return final_state["context"].final_decision

    def stream(self, application: LoanApplication) -> Iterator[Tuple[str, PipelineContext]]:
        """流式执行，逐个节点返回 (节点名, 上下文)，供实时展示。"""
        state, config = self._initial_state(application)
        for chunk in self.graph.stream(state, config, stream_mode="updates"):
            for node_name, update in chunk.items():
                yield node_name, update.get("context", state["context"])

    async def arun(self, application: LoanApplication) -> FinalDecision:
        """异步执行完整流程（高并发流水线），返回最终决策。"""
        state, config = self._initial_state(application)
        final_state = await self.agraph.ainvoke(state, config)
        return final_state["context"].final_decision

    def astream(self, application: LoanApplication) -> AsyncIterator[Tuple[str, PipelineContext]]:
        """异步流式执行，逐节点yield (节点名, 上下文)，供实时展示与SSE推送。"""
        state, config = self._initial_state(application)

        async def gen():
            async for chunk in self.agraph.astream(state, config, stream_mode="updates"):
                for node_name, update in chunk.items():
                    yield node_name, update.get("context", state["context"])

        return gen()


_orchestrator: Optional[CreditRiskOrchestrator] = None


def get_orchestrator() -> CreditRiskOrchestrator:
    """获取编排器单例。"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = CreditRiskOrchestrator()
    return _orchestrator
