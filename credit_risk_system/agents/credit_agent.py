"""信用评估Agent（风控岗）。

核心能力：征信报告解析、还款能力评估、负债率计算、
信用评分卡模型、收入负债比测算、额度初判。
输出：CreditResult（信用评分 + 额度初判）。
"""
from typing import Any, ClassVar, Dict

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.adapters import get_pai
from credit_risk_system.core.data_layer import get_mysql_store
from credit_risk_system.core.rule_engine import get_rule_engine
from credit_risk_system.core.score_card import get_scorecard
from credit_risk_system.models.risk import (
    AgentStatus,
    CreditGrade,
    CreditReportSummary,
    CreditResult,
    NextAction,
    PipelineContext,
    RepaymentAbility,
    ScoreCardResult,
)


def monthly_payment_factor(annual_rate: float, term_months: int) -> float:
    """等额本息月供系数：月供 = 本金 × factor。"""
    monthly = annual_rate / 100 / 12
    return monthly / (1 - (1 + monthly) ** (-term_months))


class CreditAgent(BaseAgent):
    name = "credit_agent"
    role = "信用评估Agent（风控岗）"
    result_model = CreditResult
    tool_names = ["query_credit_report", "compute_scorecard", "assess_repayment_ability"]

    # ----------------------------------------------------------
    # 证据采集
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        app = ctx.application
        report_raw = get_mysql_store().get_credit_report(app.applicant.id_number)
        report = CreditReportSummary(**report_raw)

        # 评分卡特征
        features = {
            "age": ctx.application.facts()["age"],
            "education": app.applicant.education.value,
            "income": app.applicant.monthly_income,
            "work_years": app.applicant.work_years,
            "marital": app.applicant.marital_status.value,
            "housing": app.applicant.housing_status.value,
            "history_years": report.first_loan_years,
            "overdue_total": report.overdue_total,
            "utilization": report.utilization,
            "query_3m": report.query_count_3m,
            "income_evidence": app.applicant.income_evidence.value,
        }
        # 模型调度：优先阿里PAI部署模型，降级本地评分卡
        pai_score = get_pai().predict_score(features)
        scorecard = get_scorecard().evaluate(features)

        # 还款能力测算（按当前等级的定价试算）
        grade = CreditGrade(scorecard.grade)
        trial_rate = rs.LPR_1Y + rs.GRADE_SPREADS.get(grade.value, 5.0)
        factor = monthly_payment_factor(trial_rate, app.loan_request.term_months)
        payment = app.loan_request.amount * factor
        existing_debt = report.used_limit * 0.05  # 已有负债按额度5%月偿还估算
        monthly_income = app.applicant.monthly_income
        dti = (existing_debt + payment) / monthly_income
        ability = RepaymentAbility(
            monthly_income=monthly_income,
            existing_monthly_debt=round(existing_debt, 2),
            new_loan_monthly_payment=round(payment, 2),
            dti=round(dti, 4),
            dti_cap=rs.DTI_CAP,
            free_cash_flow=round(monthly_income - existing_debt - payment, 2),
            assessment="",
        )

        return {
            "credit_report": report.model_dump(mode="json"),
            "features": features,
            "scorecard": scorecard.model_dump(mode="json"),
            "pai_score": pai_score,
            "repayment_ability": ability.model_dump(mode="json"),
            "trial_rate": trial_rate,
        }

    # ----------------------------------------------------------
    # LLM提示词：见 resume/prompts.py（credit_agent套装），
    # 由 BaseAgent.build_prompt 默认实现自动取用
    # ----------------------------------------------------------
    # 规则引擎决策
    # ----------------------------------------------------------
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> CreditResult:
        app = ctx.application
        report = CreditReportSummary(**evidence["credit_report"])
        scorecard_model = ScoreCardResult(**evidence["scorecard"])

        # 模型调度：PAI评分优先，评分卡兜底
        if evidence.get("pai_score") is not None:
            credit_score = int(evidence["pai_score"])
            grade = self._grade_of(credit_score)
        else:
            credit_score = scorecard_model.total_score
            grade = scorecard_model.grade

        # 还款能力与额度初判
        ability = RepaymentAbility(**evidence["repayment_ability"])
        income_multiple = rs.INCOME_MULTIPLES.get(grade.value, 0)
        # 按等级重新定价后计算月供约束下的最大可贷本金
        final_rate = rs.LPR_1Y + rs.GRADE_SPREADS.get(grade.value, 5.0)
        allowed_payment = max(0.0, app.applicant.monthly_income * rs.DTI_CAP
                              - ability.existing_monthly_debt)
        factor = monthly_payment_factor(final_rate, app.loan_request.term_months)
        payment_capped = allowed_payment / factor if factor else 0.0
        suggested = min(app.loan_request.amount,
                        app.applicant.monthly_income * income_multiple,
                        payment_capped)
        suggested = int(suggested // 10000 * 10000) if suggested >= 10000 else int(suggested)

        ability.dti_cap = rs.DTI_CAP
        ability.assessment = (
            f"月供{ability.new_loan_monthly_payment:.0f}元，收入负债比{ability.dti:.1%}"
            f"（上限{rs.DTI_CAP:.0%}）")

        # 硬性信用拒绝规则
        facts = self._facts(ctx, evidence,
                            overdue_m3=report.overdue_m3, overdue_m2=report.overdue_m2,
                            dti=ability.dti, grade=grade.value)
        violations = [r for r in get_rule_engine("credit").evaluate(facts)
                      if r.triggered]
        if violations:
            return CreditResult(
                status=AgentStatus.REJECT,
                next_action=NextAction.STOP,
                summary=f"信用硬拒绝：{'、'.join(r.rule.name for r in violations)}",
                detail="信用评估未通过，终止审批",
                credit_score=credit_score,
                credit_grade=grade,
                scorecard=scorecard_model,
                repayment_ability=ability,
                credit_report=report,
                suggested_max_amount=0,
                income_multiple=income_multiple,
            )
        return CreditResult(
            status=AgentStatus.PASS,
            next_action=NextAction.CONTINUE,
            summary=f"信用等级{grade.value}（评分{credit_score}），建议额度上限{suggested}元",
            detail=ability.assessment,
            credit_score=credit_score,
            credit_grade=grade,
            scorecard=scorecard_model,
            repayment_ability=ability,
            credit_report=report,
            suggested_max_amount=suggested,
            income_multiple=income_multiple,
        )

    @staticmethod
    def _grade_of(score: int) -> CreditGrade:
        for threshold, grade in rs.GRADE_BANDS:
            if score >= threshold:
                return CreditGrade(grade)
        return CreditGrade.E

    # ----------------------------------------------------------
    # 默认兜底（保守：转人工核定额度）
    # ----------------------------------------------------------
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> CreditResult:
        return CreditResult(
            status=AgentStatus.MANUAL_REVIEW,
            next_action=NextAction.ESCALATE,
            summary="信用评估系统异常，转人工风控岗核定",
            detail="LLM与规则引擎均不可用，按保守策略转人工",
            credit_score=0,
            credit_grade=CreditGrade.E,
            scorecard=ScoreCardResult(total_score=0, grade=CreditGrade.E,
                                      model_version="fallback"),
            repayment_ability=RepaymentAbility(assessment="人工核定"),
            credit_report=CreditReportSummary(has_report=False),
            suggested_max_amount=0,
            income_multiple=0,
        )
