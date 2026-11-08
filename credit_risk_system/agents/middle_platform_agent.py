"""风控中台Agent（风控主管/总调度）。

核心能力：全局流程编排、数据分发、规则配置管理、模型调度、
风控效果分析、异常升级处理、决策总控、策略迭代。
输出：FinalDecision（决策总控）。
"""
import json
from datetime import datetime
from typing import Any, ClassVar, Dict, Optional

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.core.data_layer import get_minio_store, get_mysql_store
from credit_risk_system.core.fraud_graph import get_fraud_graph
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.memory import get_agent_memory
from credit_risk_system.core.message_bus import EventEnvelope, get_message_bus
from credit_risk_system.core.tools import register_application
from credit_risk_system.models.risk import (
    FinalDecision,
    FinalStatus,
    PipelineContext,
)


class MiddlePlatformAgent(BaseAgent):
    """风控中台Agent：编排入口与决策总控（非LLM驱动，纯确定性逻辑）。"""

    name = "middle_platform_agent"
    role = "风控中台Agent（风控主管）"
    result_model = FinalDecision
    tool_names: ClassVar[list] = []

    def __init__(self):
        super().__init__()

    # ----------------------------------------------------------
    # 编排入口：受理与数据分发
    # ----------------------------------------------------------
    def dispatch(self, ctx: PipelineContext) -> PipelineContext:
        app = ctx.application
        self.logger.info("受理申请单 %s | 渠道=%s | 金额=%.0f | 期限=%d月",
                         app.application_id, app.channel.value,
                         app.loan_request.amount, app.loan_request.term_months)

        # 数据分发：注册申请单 + 写入知识图谱 + 基础特征注入
        register_application(app)
        get_fraud_graph().add_application(app)
        ctx.evidence["base"] = app.facts()

        # 审计留痕 + 消息总线
        ctx.record_event(self.name, "system",
                         f"受理进件：{app.application_id}（渠道{app.channel.value}）")
        try:
            get_message_bus().publish(EventEnvelope(
                topic="risk.intake", source=self.name,
                application_id=app.application_id,
                event_type="APPLICATION_RECEIVED",
                payload={"amount": app.loan_request.amount,
                         "channel": app.channel.value},
            ))
        except Exception as e:
            self.logger.warning("受理事件发布失败: %s", e)
        return ctx

    # ----------------------------------------------------------
    # 决策总控：汇总各环节结论 + 冲突裁决 + 持久化
    # ----------------------------------------------------------
    def finalize(self, ctx: PipelineContext) -> FinalDecision:
        app = ctx.application
        intake = ctx.intake_result
        antifraud = ctx.antifraud_result
        credit = ctx.credit_result
        approval = ctx.approval_result
        compliance = ctx.compliance_result
        postloan = ctx.postloan_result

        reasons: list = []
        stage_results: Dict[str, str] = {}

        # ---------- 汇总各环节结论 ----------
        for name, result in (("intake", intake), ("antifraud", antifraud),
                             ("credit", credit), ("approval", approval),
                             ("compliance", compliance), ("postloan", postloan)):
            if result is not None:
                stage_results[name] = result.status.value

        # ---------- 决策总控（冲突裁决） ----------
        decision: FinalStatus
        final_amount: Optional[float] = None
        final_rate: Optional[float] = None
        term_months: Optional[int] = app.loan_request.term_months
        supplement_notice: Optional[str] = None
        fraud_tags_listed = False   # 反欺诈信号是否已写入理由链

        if intake is not None and intake.status.value == "SUPPLEMENT":
            decision = FinalStatus.SUPPLEMENT
            supplement_notice = f"请补充资料：{'、'.join(intake.missing_documents)}"
            reasons.append(intake.summary)
        elif intake is not None and intake.status.value == "REJECT":
            decision = FinalStatus.REJECTED
            reasons.append(f"进件审核拒绝：{intake.summary}")
        elif antifraud is not None and antifraud.status.value == "REJECT":
            decision = FinalStatus.REJECTED
            reasons.append(f"反欺诈拒绝：{antifraud.summary}")
            reasons.extend(antifraud.fraud_tags[:5])
            fraud_tags_listed = True
        elif approval is not None:
            final_amount = approval.approved_amount
            final_rate = approval.approved_rate
            term_months = approval.term_months
            reasons.extend(approval.reasons)
            if approval.decision.value == "AUTO_APPROVE":
                decision = FinalStatus.APPROVED
            elif approval.decision.value == "AUTO_REJECT":
                decision = FinalStatus.REJECTED
            else:
                decision = FinalStatus.MANUAL_REVIEW
                reasons.append("转人工审批岗复核")
        else:
            decision = FinalStatus.MANUAL_REVIEW
            reasons.append("流程异常，转人工")

        # ---------- 反欺诈命中信号全程可见 ----------
        # 设备ROOT/模拟器/代理VPN、多头、团伙等信号即使未达拒绝线，
        # 也作为理由写入最终决策（拒绝路径已逐条列出，此处补齐其余路径）
        if (not fraud_tags_listed and antifraud is not None and antifraud.fraud_tags
                and decision is not FinalStatus.SUPPLEMENT):
            reasons.append("反欺诈命中信号：" + "、".join(antifraud.fraud_tags))

        # ---------- 合规一票否决 ----------
        compliance_passed = compliance.passed if compliance else True
        if compliance is not None and not compliance.passed:
            violations = [i for i in compliance.issues if i.level == "VIOLATION"]
            if violations and decision is not FinalStatus.SUPPLEMENT:
                if decision is FinalStatus.APPROVED:
                    decision = FinalStatus.REJECTED
                    final_amount = None   # 否决后不再放款，清空额度与利率
                    final_rate = None
                    reasons.append("合规违规一票否决：" + "；".join(
                        i.description for i in violations))
                else:
                    reasons.append("合规违规：" + "；".join(
                        i.description for i in violations))
                compliance_passed = False

        # ---------- 组装最终决策 ----------
        audit_ref = f"AUD-{app.application_id}-{datetime.now():%Y%m%d%H%M%S}"
        final = FinalDecision(
            application_id=app.application_id,
            decision=decision,
            final_amount=final_amount,
            final_rate=final_rate,
            term_months=term_months,
            reasons=reasons,
            fraud_score=antifraud.fraud_score if antifraud is not None else None,
            fraud_tags=list(antifraud.fraud_tags) if antifraud is not None else [],
            compliance_passed=compliance_passed,
            supplement_notice=supplement_notice,
            stage_results=stage_results,
            audit_ref=audit_ref,
            decided_at=datetime.now(),
            decided_by=self.name,
        )
        ctx.final_decision = final
        ctx.record_event(
            self.name, "system",
            f"最终决策：{decision.value}"
            + (f"（额度{final_amount:.0f}元，年化{final_rate}%）"
               if final_amount and final_rate else ""),
            level="INFO" if decision is FinalStatus.APPROVED else "WARNING",
        )

        # ---------- 持久化 + 消息发布 + 告警 ----------
        self._persist(ctx, final)
        return final

    def _persist(self, ctx: PipelineContext, final: FinalDecision) -> None:
        """决策持久化：MySQL + MinIO审计件 + 消息总线 + 跨申请记忆。"""
        app = ctx.application
        try:
            get_mysql_store().save_application(app)
            get_mysql_store().save_decision(final)
        except Exception as e:
            self.logger.warning("MySQL持久化失败: %s", e)

        try:
            audit_payload = json.dumps({
                "final_decision": final.model_dump(mode="json"),
                "audit_events": [e.model_dump(mode="json") for e in ctx.audit_events],
            }, ensure_ascii=False, indent=2, default=str)
            get_minio_store().save_bytes(
                f"audit/{app.application_id}/{final.audit_ref}.json",
                audit_payload.encode("utf-8"))
        except Exception as e:
            self.logger.warning("MinIO审计件保存失败: %s", e)

        bus = get_message_bus()
        try:
            bus.publish(EventEnvelope(
                topic="risk.decision", source=self.name,
                application_id=app.application_id,
                event_type=final.decision.value,
                payload=final.model_dump(mode="json"),
            ))
            if final.decision.value in ("REJECTED", "MANUAL_REVIEW", "SUPPLEMENT"):
                bus.publish(EventEnvelope(
                    topic="risk.alerts", source=self.name,
                    application_id=app.application_id,
                    event_type="ESCALATION",
                    payload={"decision": final.decision.value,
                             "reasons": final.reasons},
                ))
        except Exception as e:
            self.logger.warning("决策事件发布失败: %s", e)

        get_agent_memory().save(final)

    # ----------------------------------------------------------
    # 实现BaseAgent抽象接口（中台不走LLM三层兜底，直接确定性决策）
    # LLM提示词：resume/prompts.py 已登记 middle_platform_agent 套装，
    # 作为「LLM决策总控」模式的保留扩展点（当前不调用）
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        return {}

    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> FinalDecision:
        return ctx.final_decision or self.finalize(ctx)

    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> FinalDecision:
        return self.finalize(ctx)
