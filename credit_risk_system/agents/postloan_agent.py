"""贷后管理Agent（贷后岗）。

核心能力：还款行为监控、逾期预警、催收策略匹配、
资产质量分析（Doris）、风险迁徙计算。
输出：PostLoanResult（贷后风险分级 + 催收方案）。
"""
from typing import Any, ClassVar, Dict

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.data_layer import get_doris_store
from credit_risk_system.models.risk import (
    AgentStatus,
    NextAction,
    PipelineContext,
    PostLoanResult,
    RiskLevel,
)


class PostLoanAgent(BaseAgent):
    name = "postloan_agent"
    role = "贷后管理Agent（贷后岗）"
    result_model = PostLoanResult
    tool_names = ["query_asset_quality"]

    # ----------------------------------------------------------
    # 证据采集
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        credit = ctx.credit_result
        antifraud = ctx.antifraud_result
        grade = credit.credit_grade.value

        # 贷后风险分级：信用等级为基准，反欺诈HIGH提升一级
        if antifraud and antifraud.fraud_level.value in ("HIGH", "SEVERE"):
            level = {"A": "B", "B": "C", "C": "D", "D": "E", "E": "E"}.get(grade, "D")
        else:
            level = grade

        asset_quality = get_doris_store().get_asset_quality(level)
        return {"risk_level": level, "grade": grade, "asset_quality": asset_quality}

    # ----------------------------------------------------------
    # LLM提示词：见 resume/prompts.py（postloan_agent套装），
    # 由 BaseAgent.build_prompt 默认实现自动取用
    # ----------------------------------------------------------
    # 规则引擎决策
    # ----------------------------------------------------------
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> PostLoanResult:
        level = RiskLevel(evidence["risk_level"])
        plan = rs.MONITORING_PLANS.get(level.value, rs.MONITORING_PLANS["C"])

        # 预估违约概率
        pd = rs.PD_BY_GRADE.get(level.value, 0.03)
        if ctx.antifraud_result and ctx.antifraud_result.fraud_level.value == "HIGH":
            pd += 0.02

        # 12个月风险迁徙（历史迁徙矩阵）
        migration = rs.MIGRATION_MATRIX.get(level.value, {})
        worse_prob = sum(v for k, v in migration.items() if k in ("D", "E") and k > level.value)

        asset = evidence["asset_quality"]
        return PostLoanResult(
            status=AgentStatus.PASS,
            next_action=NextAction.FINALIZE,
            summary=f"贷后风险等级{level.value}，采用「{plan['strategy']}」方案",
            detail=(f"监控频率{plan['frequency']}；该等级历史30天逾期率"
                    f"{asset.get('delinquency30', 0):.1%}、90天逾期率"
                    f"{asset.get('delinquency90', 0):.1%}"),
            risk_level=level,
            monitoring_frequency=plan["frequency"],
            collection_strategy=plan["strategy"],
            alert_thresholds=list(plan["thresholds"]),
            expected_migration=f"未来12个月迁徙至更差等级概率约{worse_prob:.1%}",
            pd_estimate=round(pd, 4),
        )

    # ----------------------------------------------------------
    # 默认兜底（保守：按最高风险等级监控）
    # ----------------------------------------------------------
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> PostLoanResult:
        return PostLoanResult(
            status=AgentStatus.PASS,
            next_action=NextAction.ESCALATE,
            summary="贷后系统异常，按E级重点监控",
            detail="保守策略：专项催收团队，每日监控",
            risk_level=RiskLevel.E,
            monitoring_frequency="每日监控",
            collection_strategy="专项催收团队",
            alert_thresholds=["到期前3天预警", "逾期即催收"],
            expected_migration="未知（系统异常）",
            pd_estimate=0.12,
        )
