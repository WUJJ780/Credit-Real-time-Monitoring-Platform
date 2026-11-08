"""审批决策Agent（审批岗）。

核心能力：综合风险评分、额度核定、利率定价、
自动审批/人工复核判断、规则冲突处理。
输出：ApprovalResult（审批决策 + 最终额度 + 利率）。
"""
from typing import Any, ClassVar, Dict

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.adapters import get_bailian
from credit_risk_system.core.rule_engine import get_rule_engine
from credit_risk_system.models.risk import (
    AgentStatus,
    ApprovalDecision,
    ApprovalResult,
    NextAction,
    PipelineContext,
)


class ApprovalAgent(BaseAgent):
    name = "approval_agent"
    role = "审批决策Agent（审批岗）"
    result_model = ApprovalResult
    tool_names = ["compute_composite_risk", "price_loan", "get_online_features"]

    # ----------------------------------------------------------
    # 证据采集
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        intake = ctx.intake_result
        antifraud = ctx.antifraud_result
        credit = ctx.credit_result

        w = rs.COMPOSITE_WEIGHTS
        intake_risk = max(0.0, 100.0 - intake.completeness_score)
        composite = (w["fraud"] * antifraud.fraud_score
                     + w["credit"] * (100 - credit.credit_score)
                     + w["intake"] * intake_risk)
        return {
            "composite_risk_score": round(composite, 2),
            "intake_risk": intake_risk,
            "fraud_score": antifraud.fraud_score,
            "fraud_level": antifraud.fraud_level.value,
            "credit_score": credit.credit_score,
            "credit_grade": credit.credit_grade.value,
            "credit_status": credit.status.value,
            "suggested_max_amount": credit.suggested_max_amount,
            "dti": credit.repayment_ability.dti,
        }

    # ----------------------------------------------------------
    # LLM提示词：见 resume/prompts.py（approval_agent套装），
    # 由 BaseAgent.build_prompt 默认实现自动取用
    # ----------------------------------------------------------
    # 规则引擎决策
    # ----------------------------------------------------------
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> ApprovalResult:
        app = ctx.application
        credit = ctx.credit_result

        # 外部决策引擎（百融）优先，失败/未配置降级本地综合评分
        bailian = get_bailian().decide(evidence)
        if bailian is not None:
            evidence["composite_risk_score"] = float(bailian.get("risk_score",
                                                evidence["composite_risk_score"]))

        facts = self._facts(ctx, evidence)
        engine = get_rule_engine("approval")
        triggered = engine.triggered(facts)
        violation_names = [r.rule.name for r in triggered if r.rule.severity == "VIOLATION"]
        warn_names = [r.rule.name for r in triggered if r.rule.severity == "WARNING"]

        # ---------------- 决策矩阵 ----------------
        reasons: list = []
        if "反欺诈一票否决" in violation_names:
            decision = ApprovalDecision.AUTO_REJECT
            reasons.append("反欺诈等级SEVERE，一票否决")
        elif "信用硬拒绝" in violation_names or credit.status.value == "REJECT":
            decision = ApprovalDecision.AUTO_REJECT
            reasons.append("信用评估硬拒绝")
        elif "综合风险拒绝" in violation_names:
            decision = ApprovalDecision.AUTO_REJECT
            reasons.append(f"综合风险分{evidence['composite_risk_score']}超过拒绝线")
        elif credit.status.value == "MANUAL_REVIEW":
            decision = ApprovalDecision.MANUAL_REVIEW
            reasons.append("信用评估环节异常降级，转人工")
        elif "反欺诈人工复核" in warn_names:
            decision = ApprovalDecision.MANUAL_REVIEW
            reasons.append("反欺诈等级MEDIUM/HIGH，转人工复核")
        elif "信用等级复核" in warn_names:
            decision = ApprovalDecision.MANUAL_REVIEW
            reasons.append("信用等级C及以下，转人工复核")
        elif "负债比复核" in warn_names:
            decision = ApprovalDecision.MANUAL_REVIEW
            reasons.append("收入负债比超上限，转人工复核")
        elif evidence["composite_risk_score"] < rs.AUTO_APPROVE_RISK:
            decision = ApprovalDecision.AUTO_APPROVE
            reasons.append(f"综合风险分{evidence['composite_risk_score']}低于自动通过线")
        else:
            decision = ApprovalDecision.MANUAL_REVIEW
            reasons.append(f"综合风险分{evidence['composite_risk_score']}处于灰色区间，转人工复核")

        # ---------------- 额度核定 ----------------
        approved_amount = None
        if decision is ApprovalDecision.AUTO_REJECT:
            approved_amount = 0.0
        elif decision is ApprovalDecision.MANUAL_REVIEW:
            approved_amount = min(app.loan_request.amount, credit.suggested_max_amount)
        else:
            approved_amount = min(app.loan_request.amount, credit.suggested_max_amount)

        # ---------------- 利率定价 ----------------
        grade = credit.credit_grade.value
        spread = rs.GRADE_SPREADS.get(grade, rs.GRADE_SPREADS["C"])
        approved_rate = None
        if decision is not ApprovalDecision.AUTO_REJECT:
            approved_rate = round(rs.LPR_1Y + spread, 4)
        pricing_basis = (f"1年期LPR({rs.LPR_1Y}%)+{grade}级加点({spread}%)"
                         if approved_rate is not None else "未定价（拒绝）")

        loan_conditions = ["放款前签署电子借款合同", "按约定用途使用贷款资金"]
        if app.applicant.income_evidence.value == "自报":
            loan_conditions.append("放款前补充有效收入证明")
        if app.loan_request.purpose.value == "经营":
            loan_conditions.append("补充营业执照并完成经营场所核验")

        rule_conflicts = []
        if decision is ApprovalDecision.MANUAL_REVIEW:
            rule_conflicts.append("自动规则与风险阈值存在冲突，交由人工复核裁决")

        status = {
            ApprovalDecision.AUTO_APPROVE: AgentStatus.PASS,
            ApprovalDecision.AUTO_REJECT: AgentStatus.REJECT,
            ApprovalDecision.MANUAL_REVIEW: AgentStatus.MANUAL_REVIEW,
        }[decision]
        return ApprovalResult(
            status=status,
            next_action=NextAction.CONTINUE,
            summary=f"审批决策：{decision.value}"
                    + (f"（额度{approved_amount:.0f}元，年化{approved_rate}%）"
                       if approved_amount else ""),
            detail="；".join(reasons),
            decision=decision,
            approved_amount=approved_amount,
            approved_rate=approved_rate,
            term_months=app.loan_request.term_months,
            composite_risk_score=evidence["composite_risk_score"],
            pricing_basis=pricing_basis,
            loan_conditions=loan_conditions,
            rule_conflicts=rule_conflicts,
            reasons=reasons,
        )

    # ----------------------------------------------------------
    # 默认兜底（保守：转人工审批）
    # ----------------------------------------------------------
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> ApprovalResult:
        return ApprovalResult(
            status=AgentStatus.MANUAL_REVIEW,
            next_action=NextAction.ESCALATE,
            summary="审批系统异常，转人工审批岗",
            detail="LLM与规则引擎均不可用，保守策略转人工审批",
            decision=ApprovalDecision.MANUAL_REVIEW,
            approved_amount=None,
            approved_rate=None,
            term_months=ctx.application.loan_request.term_months,
            composite_risk_score=100.0,
            pricing_basis="人工定价",
            loan_conditions=[],
            rule_conflicts=["系统异常，人工裁决"],
            reasons=["审批决策系统异常降级"],
        )
