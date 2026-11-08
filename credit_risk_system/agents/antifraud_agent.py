"""反欺诈Agent（反欺诈岗）。

核心能力：黑名单匹配、多头借贷检测、设备风险识别、
团伙欺诈识别（Neo4j知识图谱）、历史欺诈案件向量比对（Milvus）、异常行为分析。
输出：AntiFraudResult（反欺诈评分 + 欺诈风险标签）。
"""
from typing import Any, Dict

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.data_layer import get_milvus_store, get_mysql_store
from credit_risk_system.core.fraud_graph import get_fraud_graph
from credit_risk_system.core.rule_engine import get_rule_engine
from credit_risk_system.core.tools import compute_device_risk_score
from credit_risk_system.models.risk import (
    AgentStatus,
    AntiFraudResult,
    BlacklistHit,
    DeviceRiskAssessment,
    FraudLevel,
    GangDetection,
    MultiLoanAssessment,
    NextAction,
    PipelineContext,
    SimilarCase,
)


def fraud_level_of(score: float) -> FraudLevel:
    for threshold, level in rs.FRAUD_BANDS:
        if score >= threshold:
            return FraudLevel(level)
    return FraudLevel.LOW


class AntiFraudAgent(BaseAgent):
    name = "antifraud_agent"
    role = "反欺诈Agent（反欺诈岗）"
    result_model = AntiFraudResult
    tool_names = ["query_blacklist", "query_multi_loan", "detect_gang", "search_similar_cases"]

    # ----------------------------------------------------------
    # 证据采集
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        app = ctx.application
        store = get_mysql_store()
        applicant = app.applicant

        # 1. 黑名单匹配（司法/失信/行内/同业）
        blacklist_hits = [
            BlacklistHit(**hit) for hit in store.get_blacklist(applicant.id_number, applicant.phone)
        ]
        blacklist_max = max(
            (rs.BLACKLIST_WEIGHTS.get(h.list_type, 30) for h in blacklist_hits), default=0)

        # 2. 多头借贷
        multi_raw = store.get_multi_loan(applicant.id_number)
        multi_loan = MultiLoanAssessment(**multi_raw)

        # 3. 设备风险
        device_score = compute_device_risk_score(app)
        d = app.device
        signals = []
        if d.is_rooted:
            signals.append("ROOT/越狱")
        if d.is_emulator:
            signals.append("模拟器")
        if d.is_proxy:
            signals.append("代理/VPN")
        if d.first_seen_days < 7:
            signals.append("新设备<7天")
        if d.account_count >= rs.DEVICE_MULTI_ACCOUNT_MIN:
            signals.append(f"单设备多账户({d.account_count})")
        if d.ip_mismatch:
            signals.append("IP归属异常")
        device_risk = DeviceRiskAssessment(
            rooted=d.is_rooted, emulator=d.is_emulator, proxy=d.is_proxy,
            ip_mismatch=d.ip_mismatch, first_seen_days=d.first_seen_days,
            account_count=d.account_count, score=device_score, signals=signals)

        # 4. 团伙欺诈识别（知识图谱）
        gang = get_fraud_graph().detect_gangs(app.application_id)

        # 5. 历史欺诈案件向量比对（Milvus）
        report = store.get_credit_report(applicant.id_number)
        sim_results = get_milvus_store().search_similar(
            app, multi_raw, report, gang.component_size, device_score, top_k=3)
        similar_cases = [SimilarCase(**s) for s in sim_results]
        similar_fraud = any(
            c.label == "fraud" and c.similarity >= rs.SIMILAR_CASE_THRESHOLD
            for c in similar_cases)

        return {
            "blacklist_hits": [h.model_dump(mode="json") for h in blacklist_hits],
            "blacklist_max": blacklist_max,
            "multi_loan": multi_loan.model_dump(mode="json"),
            "device_risk": device_risk.model_dump(mode="json"),
            "gang": gang.model_dump(mode="json"),
            "similar_cases": similar_cases,
            "similar_fraud": similar_fraud,
        }

    # ----------------------------------------------------------
    # LLM提示词：见 resume/prompts.py（antifraud_agent套装），
    # 由 BaseAgent.build_prompt 默认实现自动取用
    # ----------------------------------------------------------
    # 规则引擎决策
    # ----------------------------------------------------------
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> AntiFraudResult:
        gang = GangDetection(**evidence["gang"])
        multi = evidence["multi_loan"]
        facts = self._facts(ctx, evidence,
                            gang_score=gang.score,
                            similar_fraud=evidence["similar_fraud"],
                            multi_platforms=multi["platform_count"],
                            query_7d=multi["query_7d"])
        engine = get_rule_engine("antifraud")
        results = engine.evaluate(facts)

        # 按模块聚合并施加模块上限，汇总欺诈分
        cat_scores = engine.score_by_category(facts)
        fraud_score = min(
            rs.FRAUD_SCORE_CAP,
            sum(min(v, rs.MODULE_CAPS.get(k, v)) for k, v in cat_scores.items()),
        )
        level = fraud_level_of(fraud_score)

        triggered = [r for r in results if r.triggered]
        tags = [r.rule.name for r in triggered]
        if evidence["blacklist_hits"] and "黑名单命中" not in tags:
            tags.insert(0, "黑名单命中")

        severe = level is FraudLevel.SEVERE
        # 摘要带上命中信号：审计事件、消息总线、决策总控均可直接看到
        # 设备ROOT/模拟器/代理等信号即使未达拒绝线也全程可追溯
        signals_text = ("，命中信号：" + "、".join(tags)) if tags else ""
        return AntiFraudResult(
            status=AgentStatus.REJECT if severe else AgentStatus.PASS,
            next_action=NextAction.STOP if severe else NextAction.CONTINUE,
            summary=(f"欺诈等级{level.value}（评分{fraud_score:.0f}），"
                     f"{'自动拒绝' if severe else '放行进入信用评估'}"
                     f"{signals_text}"),
            detail="命中信号：" + ("、".join(tags) if tags else "无"),
            fraud_score=round(fraud_score, 1),
            fraud_level=level,
            fraud_tags=tags,
            blacklist_hits=[BlacklistHit(**h) for h in evidence["blacklist_hits"]],
            multi_loan=MultiLoanAssessment(**evidence["multi_loan"]),
            device_risk=DeviceRiskAssessment(**evidence["device_risk"]),
            gang=gang,
            similar_cases=evidence["similar_cases"],
        )

    # ----------------------------------------------------------
    # 默认兜底（保守：升级人工，不自动放行）
    # ----------------------------------------------------------
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> AntiFraudResult:
        return AntiFraudResult(
            status=AgentStatus.PASS,
            next_action=NextAction.ESCALATE,
            summary="反欺诈系统异常，升级人工复核",
            detail="LLM与规则引擎均不可用，按HIGH等级转人工反欺诈岗",
            fraud_score=60.0,
            fraud_level=FraudLevel.HIGH,
            fraud_tags=["系统降级转人工"],
            multi_loan=MultiLoanAssessment(),
            device_risk=DeviceRiskAssessment(score=0, signals=["未知"]),
            gang=GangDetection(),
        )
