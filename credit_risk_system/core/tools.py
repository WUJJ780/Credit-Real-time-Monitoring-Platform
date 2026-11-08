"""工具注册表：Function Calling工具 + MCP工具统一注册与执行。

每个工具: name / description / args_model(Pydantic参数模型) / fn(执行函数)
Agent的LLM层通过 bind_tools 获得工具schema；本地确定性路径也可直接调用execute_tool。
MCP工具以 mcp__<server>__<tool> 命名空间并入同一注册表。
"""
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core import mcp_client
from credit_risk_system.core.adapters import (
    get_feast,
    get_ocr_adapter,
)
from credit_risk_system.core.data_layer import (
    get_milvus_store,
    get_mysql_store,
)
from credit_risk_system.core.fraud_graph import get_fraud_graph
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.rule_engine import get_rule_engine
from credit_risk_system.core.score_card import get_scorecard
from credit_risk_system.models.loan import LoanApplication

logger = get_logger("tools")

# 申请单注册表（供按application_id检索的工具使用）
# 高并发下多个流水线任务（线程池/事件循环）并发注册与读取，加锁保护
_APP_REGISTRY: Dict[str, LoanApplication] = {}
_registry_lock = threading.Lock()


def register_application(app: LoanApplication) -> None:
    with _registry_lock:
        _APP_REGISTRY[app.application_id] = app


def get_application(application_id: str) -> Optional[LoanApplication]:
    with _registry_lock:
        return _APP_REGISTRY.get(application_id)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: Type[BaseModel]
    fn: Callable[..., Any]


# ============================================================
# 参数模型
# ============================================================
class _BlacklistArgs(BaseModel):
    id_number: str = Field(description="身份证号")
    phone: str = Field(default="", description="手机号")


class _MultiLoanArgs(BaseModel):
    id_number: str = Field(description="身份证号")


class _VerifyIdentityArgs(BaseModel):
    name: str = Field(description="姓名")
    id_number: str = Field(description="身份证号")
    phone: str = Field(description="手机号")


class _OcrArgs(BaseModel):
    file_key: str = Field(description="MinIO文件对象键")


class _GangArgs(BaseModel):
    application_id: str = Field(description="申请单号")


class _CreditReportArgs(BaseModel):
    id_number: str = Field(description="身份证号")


class _ScorecardArgs(BaseModel):
    features: Dict[str, Any] = Field(description="评分卡特征字典")


class _RepayAbilityArgs(BaseModel):
    monthly_income: float = Field(description="月收入")
    existing_monthly_debt: float = Field(description="已有月负债")
    amount: float = Field(description="申请金额")
    term_months: int = Field(description="期限(月)")
    annual_rate: float = Field(description="年化利率(%)")


class _CompositeArgs(BaseModel):
    fraud_score: float = Field(description="反欺诈分(0-100)")
    credit_score: float = Field(description="信用分(0-100)")
    intake_risk: float = Field(description="进件风险分(0-100)")


class _PricingArgs(BaseModel):
    grade: str = Field(description="信用等级A-E")
    amount: float = Field(description="额度")


class _RateComplianceArgs(BaseModel):
    annual_rate: Optional[float] = Field(default=None, description="年化利率(%)")


class _PurposeArgs(BaseModel):
    memo: str = Field(default="", description="用途说明")
    purpose: str = Field(default="", description="资金用途")


class _SimilarCaseArgs(BaseModel):
    application_id: str = Field(description="申请单号")


class _FeastArgs(BaseModel):
    application_id: str = Field(description="申请单号")


class _AssetQualityArgs(BaseModel):
    grade: str = Field(description="贷后风险等级A-E")


class _IntakeRulesArgs(BaseModel):
    facts: Dict[str, Any] = Field(description="进件标准化事实字典")


# ============================================================
# 工具实现
# ============================================================
def _query_blacklist(args: _BlacklistArgs) -> dict:
    hits = get_mysql_store().get_blacklist(args.id_number, args.phone)
    return {"hits": hits, "count": len(hits)}


def _query_multi_loan(args: _MultiLoanArgs) -> dict:
    return get_mysql_store().get_multi_loan(args.id_number)


def _verify_three_elements(args: _VerifyIdentityArgs) -> dict:
    ok = get_mysql_store().verify_identity(args.name, args.id_number, args.phone)
    return {"verified": ok}


def _ocr_document(args: _OcrArgs) -> dict:
    return get_ocr_adapter().recognize(args.file_key)


def _detect_gang(args: _GangArgs) -> dict:
    result = get_fraud_graph().detect_gangs(args.application_id)
    return result.model_dump(mode="json")


def _query_credit_report(args: _CreditReportArgs) -> dict:
    return get_mysql_store().get_credit_report(args.id_number)


def _compute_scorecard(args: _ScorecardArgs) -> dict:
    result = get_scorecard().evaluate(args.features)
    return result.model_dump(mode="json")


def _assess_repayment(args: _RepayAbilityArgs) -> dict:
    monthly_rate = args.annual_rate / 100 / 12
    factor = monthly_rate / (1 - (1 + monthly_rate) ** (-args.term_months))
    payment = args.amount * factor
    dti = (args.existing_monthly_debt + payment) / args.monthly_income
    return {
        "new_loan_monthly_payment": round(payment, 2),
        "dti": round(dti, 4),
        "dti_cap": rs.DTI_CAP,
        "free_cash_flow": round(args.monthly_income - args.existing_monthly_debt - payment, 2),
    }


def _compute_composite(args: _CompositeArgs) -> dict:
    w = rs.COMPOSITE_WEIGHTS
    score = (w["fraud"] * args.fraud_score + w["credit"] * (100 - args.credit_score)
             + w["intake"] * args.intake_risk)
    return {"composite_risk_score": round(score, 2),
            "weights": w,
            "auto_approve_threshold": rs.AUTO_APPROVE_RISK,
            "auto_reject_threshold": rs.AUTO_REJECT_RISK}


def _price_loan(args: _PricingArgs) -> dict:
    grade = args.grade.upper()
    spread = rs.GRADE_SPREADS.get(grade, rs.GRADE_SPREADS["C"])
    rate = round(rs.LPR_1Y + spread, 4)
    return {"annual_rate": rate, "lpr": rs.LPR_1Y, "spread": spread, "grade": grade}


def _check_rate_compliance(args: _RateComplianceArgs) -> dict:
    rate = args.annual_rate
    if rate is None:
        return {"passed": True, "note": "无定价（未进入审批定价环节）"}
    issues = []
    if rate > rs.LPR_1Y * rs.RATE_CAP_LPR_MULTIPLE:
        issues.append(f"年化利率{rate}%超过4倍LPR司法保护上限"
                      f"({rs.LPR_1Y * rs.RATE_CAP_LPR_MULTIPLE}%)")
    if rate > rs.RATE_CAP_ABSOLUTE:
        issues.append(f"年化利率{rate}%超过24%上限")
    return {"passed": not issues, "issues": issues, "annual_rate": rate}


def _check_purpose_compliance(args: _PurposeArgs) -> dict:
    keywords = [kw for kw in rs.PROHIBITED_PURPOSE_KEYWORDS
                if kw in args.memo or kw in args.purpose]
    return {"passed": not keywords, "hit_keywords": keywords}


def _search_similar_cases(args: _SimilarCaseArgs) -> dict:
    app = get_application(args.application_id)
    if app is None:
        return {"error": "申请单未注册"}
    store = get_mysql_store()
    multi_loan = store.get_multi_loan(app.applicant.id_number)
    report = store.get_credit_report(app.applicant.id_number)
    gang = get_fraud_graph().detect_gangs(app.application_id)
    device_score = compute_device_risk_score(app)
    results = get_milvus_store().search_similar(
        app, multi_loan, report, gang.component_size, device_score, top_k=3)
    return {"similar_cases": results}


def compute_device_risk_score(app: LoanApplication) -> int:
    """设备风险评分（信号累加，上限见MODULE_CAPS['device']）。"""
    d = app.device
    score = 0
    if d.is_rooted:
        score += rs.DEVICE_SIGNALS[0][1]
    if d.is_emulator:
        score += rs.DEVICE_SIGNALS[1][1]
    if d.is_proxy:
        score += rs.DEVICE_SIGNALS[2][1]
    if d.first_seen_days < 7:
        score += rs.DEVICE_SIGNALS[3][1]
    elif d.first_seen_days < 30:
        score += rs.DEVICE_SIGNALS[4][1]
    if d.account_count >= rs.DEVICE_MULTI_ACCOUNT_MIN:
        score += rs.DEVICE_SIGNALS[5][1]
    if d.ip_mismatch:
        score += rs.DEVICE_SIGNALS[6][1]
    return min(score, rs.MODULE_CAPS["device"])


def _get_online_features(args: _FeastArgs) -> dict:
    app = get_application(args.application_id)
    if app is None:
        return {"error": "申请单未注册"}
    return get_feast().get_online_features(app)


def _query_asset_quality(args: _AssetQualityArgs) -> dict:
    from credit_risk_system.core.data_layer import get_doris_store
    return get_doris_store().get_asset_quality(args.grade)


def _evaluate_intake_rules(args: _IntakeRulesArgs) -> dict:
    results = get_rule_engine("intake").evaluate(args.facts)
    return {"triggered": [r.rule.name for r in results if r.triggered],
            "violations": [r.rule.name for r in results if r.triggered
                           and r.rule.severity == "VIOLATION"]}


# ============================================================
# 注册表
# ============================================================
TOOLS: Dict[str, ToolSpec] = {
    spec.name: spec for spec in (
        ToolSpec("query_blacklist", "查询司法/失信/行内/同业黑名单", _BlacklistArgs, _query_blacklist),
        ToolSpec("query_multi_loan", "查询多头借贷数据（平台数/近7日查询/余额）", _MultiLoanArgs, _query_multi_loan),
        ToolSpec("verify_three_elements", "三要素核验（姓名/身份证/手机号）", _VerifyIdentityArgs, _verify_three_elements),
        ToolSpec("ocr_document", "OCR识别证件影像（百度OCR）", _OcrArgs, _ocr_document),
        ToolSpec("detect_gang", "知识图谱团伙欺诈检测", _GangArgs, _detect_gang),
        ToolSpec("query_credit_report", "查询征信报告摘要", _CreditReportArgs, _query_credit_report),
        ToolSpec("compute_scorecard", "信用评分卡模型评分", _ScorecardArgs, _compute_scorecard),
        ToolSpec("assess_repayment_ability", "还款能力评估（月供/收入负债比/自由现金流）",
                 _RepayAbilityArgs, _assess_repayment),
        ToolSpec("compute_composite_risk", "综合风险分计算（反欺诈+信用+进件加权）",
                 _CompositeArgs, _compute_composite),
        ToolSpec("price_loan", "利率定价（LPR+信用等级加点）", _PricingArgs, _price_loan),
        ToolSpec("check_rate_compliance", "利率合规校验（4倍LPR/24%上限）",
                 _RateComplianceArgs, _check_rate_compliance),
        ToolSpec("check_purpose_compliance", "资金用途合规校验", _PurposeArgs, _check_purpose_compliance),
        ToolSpec("search_similar_cases", "Milvus检索历史相似欺诈案件", _SimilarCaseArgs, _search_similar_cases),
        ToolSpec("get_online_features", "获取Feast在线特征", _FeastArgs, _get_online_features),
        ToolSpec("query_asset_quality", "查询资产质量（Doris）", _AssetQualityArgs, _query_asset_quality),
        ToolSpec("evaluate_intake_rules", "进件准入规则评估", _IntakeRulesArgs, _evaluate_intake_rules),
    )
}


def get_tool_schemas(names: Optional[List[str]] = None) -> List[dict]:
    """生成OpenAI Function Calling工具schema。"""
    selected = [TOOLS[n] for n in names] if names else list(TOOLS.values())
    schemas = []
    for spec in selected:
        schemas.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args_model.model_json_schema(),
            },
        })
    # 合并MCP工具
    for full_name, ref in mcp_client.get_mcp_tools().items():
        schemas.append({
            "type": "function",
            "function": {
                "name": full_name,
                "description": ref.description,
                "parameters": ref.input_schema,
            },
        })
    return schemas


def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    """执行工具（含MCP工具），返回JSON字符串。"""
    try:
        if name.startswith("mcp__"):
            return mcp_client.call_mcp_tool(name, arguments)
        spec = TOOLS.get(name)
        if spec is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
        args = spec.args_model(**arguments)
        result = spec.fn(args)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning("工具[%s]执行失败: %s", name, e)
        return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)


async def aexecute_tool(name: str, arguments: Dict[str, Any]) -> str:
    """异步执行工具：卸载到受控线程池（阻塞SDK不卡事件循环）。"""
    from credit_risk_system.core.async_utils import run_blocking

    return await run_blocking(execute_tool, name, arguments)
