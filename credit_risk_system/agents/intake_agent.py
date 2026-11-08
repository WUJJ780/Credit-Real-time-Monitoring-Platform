"""进件审核Agent（初审岗）。

核心能力：资料完整性校验、身份核验（三要素）、OCR识别、基础准入规则校验。
输出：IntakeResult（通过/拒绝/补件）。
"""
from typing import Any, ClassVar, Dict

from credit_risk_system.agents.base_agent import BaseAgent
from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.adapters import get_ocr_adapter
from credit_risk_system.core.data_layer import get_mysql_store
from credit_risk_system.core.rule_engine import get_rule_engine
from credit_risk_system.models.risk import (
    AgentStatus,
    IntakeResult,
    NextAction,
    PipelineContext,
)


class IntakeAgent(BaseAgent):
    name = "intake_agent"
    role = "进件审核Agent（初审岗）"
    result_model = IntakeResult
    tool_names = ["ocr_document", "verify_three_elements", "evaluate_intake_rules"]

    # ----------------------------------------------------------
    # 证据采集
    # ----------------------------------------------------------
    def collect_evidence(self, ctx: PipelineContext) -> Dict[str, Any]:
        app = ctx.application
        store = get_mysql_store()
        ocr = get_ocr_adapter()

        # OCR识别全部影像资料
        ocr_results = {doc.doc_type.value: ocr.recognize(doc.file_key)
                       for doc in app.documents}
        confidences = [r["confidence"] for r in ocr_results.values()]
        ocr_confidence = min(confidences) if confidences else 0.0

        # 三要素核验
        identity_verified = store.verify_identity(
            app.applicant.name, app.applicant.id_number, app.applicant.phone)

        # 资料完整性
        uploaded = {doc.doc_type for doc in app.documents if doc.uploaded}
        missing = [d.value for d in rs.REQUIRED_DOCS if d not in uploaded]
        has_business_license = rs.BUSINESS_LICENSE_DOC in uploaded

        return {
            "ocr_results": ocr_results,
            "ocr_confidence": round(ocr_confidence, 3),
            "identity_verified": identity_verified,
            "missing_docs": missing,
            "has_business_license": has_business_license,
        }

    # ----------------------------------------------------------
    # LLM提示词：见 resume/prompts.py（intake_agent套装），
    # 由 BaseAgent.build_prompt 默认实现自动取用
    # ----------------------------------------------------------
    # 规则引擎决策
    # ----------------------------------------------------------
    def rule_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> IntakeResult:
        app = ctx.application
        facts = self._facts(ctx, evidence)

        # 阿里云规则引擎（配置后云端评估，否则本地规则引擎），结果统一为
        # [{"name","severity","message"}]（仅命中项）
        triggered = self._evaluate_rules(facts)

        violations = [r["name"] for r in triggered if r["severity"] == "VIOLATION"]
        warnings = [r["name"] for r in triggered if r["severity"] == "WARNING"]
        rules_triggered = [r["name"] for r in triggered]

        completeness = round(sum(
            rs.DOC_WEIGHTS[d] for d in rs.REQUIRED_DOCS
            if d.value not in evidence["missing_docs"]) / 1.0, 1)
        missing = evidence["missing_docs"]

        if violations:
            return IntakeResult(
                status=AgentStatus.REJECT,
                next_action=NextAction.STOP,
                summary=f"准入拒绝：命中{len(violations)}条硬性规则（{'、'.join(violations)}）",
                detail=f"申请人{app.applicant.name}不符合基础准入条件",
                completeness_score=completeness,
                identity_verified=evidence["identity_verified"],
                ocr_confidence=evidence["ocr_confidence"],
                missing_documents=missing,
                warnings=warnings,
                rules_triggered=rules_triggered,
            )
        if missing:
            return IntakeResult(
                status=AgentStatus.SUPPLEMENT,
                next_action=NextAction.SUPPLEMENT,
                summary=f"资料不完整，需补件：{'、'.join(missing)}",
                detail="补件完成后重新发起进件审核",
                completeness_score=completeness,
                identity_verified=evidence["identity_verified"],
                ocr_confidence=evidence["ocr_confidence"],
                missing_documents=missing,
                warnings=warnings,
                rules_triggered=rules_triggered,
            )
        return IntakeResult(
            status=AgentStatus.PASS,
            next_action=NextAction.CONTINUE,
            summary=f"进件审核通过（完整度{completeness}分，三要素{'通过' if evidence['identity_verified'] else '不通过'}）",
            detail=f"OCR置信度{evidence['ocr_confidence']}" + ("；" + "；".join(warnings) if warnings else ""),
            completeness_score=completeness,
            identity_verified=evidence["identity_verified"],
            ocr_confidence=evidence["ocr_confidence"],
            missing_documents=missing,
            warnings=warnings,
            rules_triggered=rules_triggered,
        )

    @staticmethod
    def _evaluate_rules(facts: Dict[str, Any]) -> list:
        """统一本地规则引擎与阿里云规则引擎的输出结构。"""
        from credit_risk_system.core.adapters import get_aliyun_rules

        remote = get_aliyun_rules().evaluate("intake", facts)
        if remote is not None:
            return [r for r in remote if r.get("triggered")]
        results = get_rule_engine("intake").evaluate(facts)
        return [{"name": r.rule.name, "severity": r.rule.severity, "message": r.rule.message}
                for r in results if r.triggered]

    # ----------------------------------------------------------
    # 默认兜底（保守：转人工初审）
    # ----------------------------------------------------------
    def default_decide(self, ctx: PipelineContext, evidence: Dict[str, Any]) -> IntakeResult:
        return IntakeResult(
            status=AgentStatus.SUPPLEMENT,
            next_action=NextAction.ESCALATE,
            summary="系统决策异常，转人工初审岗复核",
            detail="LLM与规则引擎均不可用，保守策略转人工",
            completeness_score=0,
            identity_verified=False,
            ocr_confidence=0,
            missing_documents=[d.value for d in rs.REQUIRED_DOCS],
            warnings=["决策降级：人工复核"],
            rules_triggered=["SYSTEM_FALLBACK"],
        )
