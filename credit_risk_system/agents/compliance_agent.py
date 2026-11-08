"""合规审计Agent（合规岗）。

核心能力：全流程合规检查、监管政策适配、利率合规校验、
数据安全审计（PIPL）、监管报送数据生成。
输出：ComplianceResult（合规报告 + 整改提示）。
合规违规（VIOLATION级）由风控中台一票否决。
"""
from typing import Any, ClassVar, Dict

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.rule_engine import get_rule_engine
from credit_risk_system.models.risk import (
    AgentStatus,
    ComplianceIssue,
    ComplianceResult,
    NextAction,
    PipelineContext,
)

_REMEDIATIONS = {
    "利率4倍LPR合规": "立即下调定价至4倍LPR以内",
    "利率24%合规": "立即下调定价至24%以内",
    "资金用途合规": "拒绝该申请并上报可疑资金用途",
    "个人信息授权": "补充个人信息处理授权书（PIPL第13条）",
    "征信查询授权": "补充征信查询授权书后重新发起",
    "拒绝留痕": "补充拒绝理由留痕",
    "审计留痕完整性": "完善各环节审计记录",
}


class ComplianceAgent(BaseAgent):
    name = "compliance_agent"
    role = "合规审计Agent（合规岗）"
    result_model = ComplianceResult
    tool_names = ["check_rate_compliance", "check_purpose_compliance"]

    # ----------------------------------------------------------
    # 证据采集
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        approval = ctx.approval_result
        app = ctx.application
        return {
            "annual_rate": approval.approved_rate if approval else None,
            "decision": (approval.decision.value if approval and approval.decision
                         else "PENDING"),
            "has_reasons": bool(approval and approval.reasons),
            "audit_count": len(ctx.audit_events),
            "purpose": app.loan_request.purpose.value,
            "memo": app.loan_request.memo or "",
            "consent": app.privacy_consent,
            "authorization": app.credit_authorization,
            "loan_conditions": approval.loan_conditions if approval else [],
        }

    # ----------------------------------------------------------
    # LLM提示词：见 resume/prompts.py（compliance_agent套装，
    # 含规则引擎实时注入的合规规则清单），
    # 由 BaseAgent.build_prompt 默认实现自动取用
    # ----------------------------------------------------------
    # 规则引擎决策
    # ----------------------------------------------------------
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> ComplianceResult:
        facts = self._facts(ctx, evidence)
        engine = get_rule_engine("compliance")
        triggered = [r for r in engine.evaluate(facts) if r.triggered]

        issues = [
            ComplianceIssue(
                rule=r.rule.name,
                level=r.rule.severity,
                description=r.rule.message,
                remediation=_REMEDIATIONS.get(r.rule.name, "转人工合规岗处理"),
            )
            for r in triggered
        ]
        violations = [i for i in issues if i.level == "VIOLATION"]
        warnings = [i for i in issues if i.level == "WARNING"]
        passed = not violations

        if not passed:
            return ComplianceResult(
                status=AgentStatus.REJECT,
                next_action=NextAction.ESCALATE,
                summary=f"合规检查不通过：{len(violations)}项违规，建议一票否决",
                detail="；".join(i.description for i in violations),
                passed=False,
                issues=issues,
                checked_rules=[r.name for r in engine.rules],
                report_ref=f"COMP-{ctx.application.application_id}",
            )
        return ComplianceResult(
            status=AgentStatus.PASS,
            next_action=NextAction.CONTINUE,
            summary=f"合规检查通过（{len(issues)}项提示）" if issues else "合规检查通过（无提示项）",
            detail="；".join(w.description for w in warnings) if warnings else "全部合规规则满足",
            passed=True,
            issues=issues,
            checked_rules=[r.name for r in engine.rules],
            report_ref=f"COMP-{ctx.application.application_id}",
        )

    # ----------------------------------------------------------
    # 默认兜底（保守：视为不合规，转人工合规岗）
    # ----------------------------------------------------------
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> ComplianceResult:
        return ComplianceResult(
            status=AgentStatus.MANUAL_REVIEW,
            next_action=NextAction.ESCALATE,
            summary="合规系统异常，转人工合规岗复核",
            detail="LLM与规则引擎均不可用，保守策略视为不合规",
            passed=False,
            issues=[ComplianceIssue(rule="系统异常", level="WARNING",
                                    description="合规检查系统不可用", remediation="人工复核")],
            checked_rules=[],
            report_ref=f"COMP-{ctx.application.application_id}",
        )
