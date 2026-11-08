"""规则引擎：确定性风控规则（银行级可解释决策的核心）。

规则为声明式定义：condition(facts) -> bool，命中后携带 score/severity。
score 支持静态值或 callable(facts)（用于动态分值，如黑名单类型权重）。

标准化事实字典(facts)键说明（各Agent在collect_evidence阶段构建）：
  基础: age income amount term purpose memo phone_months education marital
        housing work_years income_evidence consent authorization channel
  进件: missing_docs ocr_confidence identity_verified
  反欺诈: blacklist_max multi_platforms query_7d rooted emulator proxy
        first_seen_days account_count ip_mismatch gang_score similar_fraud
  信用: has_report history_years overdue_m1 overdue_m2 overdue_m3 overdue_total
        utilization query_3m credit_cards dti free_cash_flow credit_score grade
  审批: fraud_score fraud_level composite_risk suggested_max
  合规: annual_rate decision audit_count
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

from credit_risk_system.config import risk_strategy as rs


# ============================================================
# 条件构造器（声明式规则）
# ============================================================
def _num(key: str) -> Callable[[dict], float]:
    return lambda f: float(f.get(key) or 0)


def between(key: str, lo: Optional[float], hi: Optional[float]) -> Callable[[dict], bool]:
    def cond(f):
        v = _num(key)(f)
        return (lo is None or v >= lo) and (hi is None or v <= hi)
    return cond


def ge(key: str, value: float) -> Callable[[dict], bool]:
    return lambda f: _num(key)(f) >= value


def lt(key: str, value: float) -> Callable[[dict], bool]:
    return lambda f: _num(key)(f) < value


def eq(key: str, value: Any) -> Callable[[dict], bool]:
    return lambda f: f.get(key) == value


def is_false(key: str) -> Callable[[dict], bool]:
    return lambda f: bool(f.get(key)) is False


def contains_keyword(key: str, keywords: tuple) -> Callable[[dict], bool]:
    return lambda f: any(kw in str(f.get(key) or "") for kw in keywords)


# ============================================================
# 规则定义
# ============================================================
@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    description: str
    condition: Callable[[Dict[str, Any]], bool]
    score: Union[float, Callable[[Dict[str, Any]], float]] = 0.0
    message: str = ""
    severity: str = "WARNING"  # INFO / WARNING / VIOLATION

    def score_of(self, facts: dict) -> float:
        return self.score(facts) if callable(self.score) else self.score


@dataclass
class RuleResult:
    rule: Rule
    triggered: bool

    @property
    def message(self) -> str:
        return self.rule.message


class RuleEngine:
    """规则引擎：批量评估规则集。"""

    def __init__(self, name: str, rules: List[Rule]):
        self.name = name
        self.rules = rules

    def evaluate(self, facts: Dict[str, Any]) -> List[RuleResult]:
        return [RuleResult(r, r.condition(facts)) for r in self.rules]

    def triggered(self, facts: Dict[str, Any]) -> List[RuleResult]:
        return [res for res in self.evaluate(facts) if res.triggered]

    def score(self, facts: Dict[str, Any]) -> float:
        return sum(res.rule.score_of(facts) for res in self.triggered(facts))

    def score_by_category(self, facts: Dict[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for res in self.triggered(facts):
            out[res.rule.category] = out.get(res.rule.category, 0) + res.rule.score_of(facts)
        return out


# ============================================================
# 各环节规则集
# ============================================================
def build_intake_rules() -> List[Rule]:
    return [
        Rule("年龄准入", "base", "年龄须在22-60周岁",
             lambda f: not between("age", rs.AGE_MIN, rs.AGE_MAX)(f),
             0, "年龄不符合准入要求", "VIOLATION"),
        Rule("收入准入", "base", "月收入不低于3000元",
             lt("income", rs.INCOME_MIN), 0, "月收入低于准入标准", "VIOLATION"),
        Rule("金额准入", "base", "申请金额在1000-50万元区间",
             lambda f: not between("amount", rs.AMOUNT_MIN, rs.AMOUNT_MAX)(f),
             0, "申请金额超出产品区间", "VIOLATION"),
        Rule("期限准入", "base", "期限在3-60个月",
             lambda f: not between("term", rs.TERM_MIN, rs.TERM_MAX)(f),
             0, "借款期限超出产品区间", "VIOLATION"),
        Rule("三要素核验", "identity", "姓名/身份证/手机号三要素一致",
             is_false("identity_verified"), 0, "三要素核验不通过", "VIOLATION"),
        Rule("影像质量", "ocr", "OCR置信度不低于0.6",
             lt("ocr_confidence", rs.OCR_CONFIDENCE_MIN), 0, "证件影像不合格，需重新上传", "VIOLATION"),
        Rule("影像质量提示", "ocr", "OCR置信度低于0.8时提示人工核验",
             lambda f: lt("ocr_confidence", rs.OCR_CONFIDENCE_WARN)(f)
             and ge("ocr_confidence", rs.OCR_CONFIDENCE_MIN)(f),
             0, "证件影像置信度偏低，建议人工复核", "WARNING"),
        Rule("手机号新注册提示", "base", "手机号注册时长不足6个月提示",
             lt("phone_months", rs.PHONE_REG_WARN_MONTHS), 0, "手机号注册时长较短，转反欺诈重点核查", "WARNING"),
        Rule("经营贷执照提示", "doc", "经营贷需补充营业执照",
             lambda f: f.get("purpose") == "经营" and f.get("has_business_license") is False,
             0, "经营用途需补充营业执照", "WARNING"),
        Rule("其他用途说明", "doc", "其他用途需提供用途说明",
             lambda f: f.get("purpose") == "其他" and not str(f.get("memo") or "").strip(),
             0, "用途为「其他」时需填写用途说明", "WARNING"),
    ]


def build_antifraud_rules() -> List[Rule]:
    return [
        Rule("黑名单命中", "blacklist", "司法/失信/行内/同业黑名单",
             ge("blacklist_max", 1),
             lambda f: float(f.get("blacklist_max") or 0), "命中黑名单", "VIOLATION"),
        Rule("多头借贷", "multiloan", "借贷平台数超阈值",
             lambda f: _num("multi_platforms")(f) >= rs.MULTILOAN_RULES[-1][0],
             lambda f: next((s for n, s in rs.MULTILOAN_RULES if _num("multi_platforms")(f) >= n), 0),
             "多头借贷平台数异常", "WARNING"),
        Rule("借贷查询频繁", "multiloan", "近7日借贷查询次数超阈值",
             lambda f: _num("query_7d")(f) >= rs.QUERY_7D_RULES[-1][0],
             lambda f: next((s for n, s in rs.QUERY_7D_RULES if _num("query_7d")(f) >= n), 0),
             "近7日借贷查询频繁", "WARNING"),
        Rule("设备ROOT/越狱", "device", "设备被ROOT或越狱",
             eq("rooted", True), rs.DEVICE_SIGNALS[0][1], "设备已ROOT/越狱", "WARNING"),
        Rule("模拟器", "device", "运行于模拟器环境",
             eq("emulator", True), rs.DEVICE_SIGNALS[1][1], "模拟器环境", "WARNING"),
        Rule("代理VPN", "device", "使用代理或VPN",
             eq("proxy", True), rs.DEVICE_SIGNALS[2][1], "代理/VPN网络", "WARNING"),
        Rule("新设备7天", "device", "设备首次出现不足7天",
             lt("first_seen_days", 7), rs.DEVICE_SIGNALS[3][1], "设备首次出现不足7天", "WARNING"),
        Rule("新设备30天", "device", "设备首次出现不足30天",
             lambda f: between("first_seen_days", 7, 30)(f), rs.DEVICE_SIGNALS[4][1],
             "设备首次出现不足30天", "INFO"),
        Rule("单设备多账户", "device", "单设备关联多个申请账户",
             ge("account_count", rs.DEVICE_MULTI_ACCOUNT_MIN),
             rs.DEVICE_SIGNALS[5][1], "单设备关联多账户", "WARNING"),
        Rule("IP归属异常", "device", "IP归属地与常驻地不一致",
             eq("ip_mismatch", True), rs.DEVICE_SIGNALS[6][1], "IP归属地与常驻地不一致", "WARNING"),
        Rule("团伙欺诈关联", "gang", "知识图谱关联已确认欺诈团伙/案件",
             ge("gang_score", rs.GANG_FRAUD_LINK_SCORE),
             lambda f: float(f.get("gang_score") or 0), "关联已确认欺诈团伙/案件", "VIOLATION"),
        Rule("共享设备", "gang", "与非欺诈申请人共享设备",
             lambda f: rs.GANG_SHARED_DEVICE_SCORE <= _num("gang_score")(f) < rs.GANG_FRAUD_LINK_SCORE,
             lambda f: float(f.get("gang_score") or 0), "与非欺诈申请人共享设备", "WARNING"),
        Rule("相似欺诈案件", "similar", "与历史欺诈案件向量相似度超阈值",
             eq("similar_fraud", True), rs.SIMILAR_FRAUD_SCORE, "与历史欺诈案件高度相似", "WARNING"),
    ]


def build_credit_rules() -> List[Rule]:
    return [
        Rule("M3+逾期", "credit", "近24个月存在M3+逾期",
             ge("overdue_m3", 1), 0, "近24个月存在M3+逾期，信用硬拒绝", "VIOLATION"),
        Rule("M2多次逾期", "credit", "近24个月M2逾期2次及以上",
             ge("overdue_m2", 2), 0, "近24个月M2逾期2次及以上，信用硬拒绝", "VIOLATION"),
        Rule("资不抵债", "credit", "收入负债比超75%硬拒绝线",
             ge("dti", rs.DTI_HARD_REJECT), 0, "收入负债比超75%，还款能力严重不足", "VIOLATION"),
        Rule("信用等级E", "credit", "信用等级为E",
             eq("grade", "E"), 0, "信用等级E，不予准入", "VIOLATION"),
    ]


def build_approval_rules() -> List[Rule]:
    return [
        Rule("反欺诈一票否决", "decision", "反欺诈等级SEVERE自动拒绝",
             eq("fraud_level", "SEVERE"), 0, "反欺诈SEVERE等级，自动拒绝", "VIOLATION"),
        Rule("信用硬拒绝", "decision", "信用评估环节拒绝",
             eq("credit_status", "REJECT"), 0, "信用评估未通过", "VIOLATION"),
        Rule("反欺诈人工复核", "decision", "反欺诈MEDIUM/HIGH转人工",
             lambda f: f.get("fraud_level") in ("MEDIUM", "HIGH"),
             0, "反欺诈风险等级较高，转人工复核", "WARNING"),
        Rule("信用等级复核", "decision", "信用等级C及以下转人工",
             lambda f: f.get("grade") in ("C", "D"),
             0, "信用等级C及以下，转人工复核", "WARNING"),
        Rule("负债比复核", "decision", "收入负债比超上限转人工",
             ge("dti", rs.DTI_CAP), 0, "收入负债比超上限，转人工复核", "WARNING"),
        Rule("综合风险拒绝", "decision", "综合风险分超自动拒绝线",
             ge("composite_risk", rs.AUTO_REJECT_RISK), 0, "综合风险分超阈值，自动拒绝", "VIOLATION"),
    ]


def build_compliance_rules() -> List[Rule]:
    return [
        Rule("利率4倍LPR合规", "rate", "年化利率不超过4倍LPR",
             lambda f: f.get("annual_rate") is not None
             and float(f["annual_rate"]) > rs.LPR_1Y * rs.RATE_CAP_LPR_MULTIPLE,
             0, "年化利率超过4倍LPR司法保护上限", "VIOLATION"),
        Rule("利率24%合规", "rate", "年化利率不超过24%",
             lambda f: f.get("annual_rate") is not None
             and float(f["annual_rate"]) > rs.RATE_CAP_ABSOLUTE,
             0, "年化利率超过24%上限", "VIOLATION"),
        Rule("资金用途合规", "purpose", "资金用途不得违反监管规定",
             contains_keyword("memo", rs.PROHIBITED_PURPOSE_KEYWORDS),
             0, "资金用途涉及监管禁止领域（股票/期货/购房等）", "VIOLATION"),
        Rule("个人信息授权", "privacy", "须获得个人信息处理授权(PIPL)",
             is_false("consent"), 0, "未获得个人信息处理授权，违反《个人信息保护法》", "VIOLATION"),
        Rule("征信查询授权", "privacy", "征信查询须获得授权",
             is_false("authorization"), 0, "未经授权查询征信报告", "VIOLATION"),
        Rule("拒绝留痕", "audit", "拒绝决定须保留完整理由",
             lambda f: f.get("decision") == "REJECTED" and not f.get("has_reasons"),
             0, "拒绝决定未留痕拒绝理由", "WARNING"),
        Rule("审计留痕完整性", "audit", "审计事件链完整",
             lt("audit_count", 3), 0, "审计留痕不完整", "WARNING"),
    ]


# ============================================================
# 引擎注册表
# ============================================================
_BUILDERS = {
    "intake": build_intake_rules,
    "antifraud": build_antifraud_rules,
    "credit": build_credit_rules,
    "approval": build_approval_rules,
    "compliance": build_compliance_rules,
}
_ENGINES: Dict[str, RuleEngine] = {}


def get_rule_engine(stage: str) -> RuleEngine:
    """获取规则引擎单例。"""
    if stage not in _ENGINES:
        if stage not in _BUILDERS:
            raise KeyError(f"未知规则集: {stage}")
        _ENGINES[stage] = RuleEngine(stage, _BUILDERS[stage]())
    return _ENGINES[stage]
