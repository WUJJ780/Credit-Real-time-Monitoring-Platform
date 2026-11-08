"""外部服务适配器：百度OCR / 阿里云规则引擎 / 百融决策引擎 / 阿里PAI / Feast特征平台。

统一约定：外部服务未配置或调用失败时返回None/降级结果，由调用方（Agent）
走本地兜底路径（本地规则引擎/评分卡），保证决策链路不中断。
"""
from typing import Any, Dict, List, Optional

from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger
from credit_risk_system.models.loan import LoanApplication

logger = get_logger("adapters")


# ============================================================
# 百度OCR识别引擎
# ============================================================
# Mock覆盖数据（演示案例：疑似翻拍的低置信度影像）
_MOCK_OCR_OVERRIDES = {
    "APP20260002/id_front.jpg": {
        "text": "姓名 陈强 性别 男 住址 广东省深圳市...（OCR识别文本）",
        "confidence": 0.68,
        "note": "身份证照片边缘模糊，疑似翻拍件",
    },
}


class BaiduOCRAdapter:
    """百度OCR识别引擎（生产环境通过HTTP调用，未配置时返回Mock结果）。"""

    def __init__(self):
        self.enabled = bool(settings.BAIDU_OCR_API_KEY)

    def recognize(self, file_key: str) -> Dict[str, Any]:
        if not self.enabled:
            override = _MOCK_OCR_OVERRIDES.get(file_key)
            if override:
                return dict(override)
            return {"text": "OCR识别文本（Mock模式，未配置百度OCR密钥）",
                    "confidence": 0.95, "note": ""}
        # 生产环境：调用百度OCR HTTP接口（此处保留扩展点）
        try:
            import httpx

            resp = httpx.post(
                "https://aip.baidubce.com/rest/2.0/ocr/v1/idcard",
                params={"access_token": settings.BAIDU_OCR_API_KEY},
                json={"image": file_key},
                timeout=10,
            )
            data = resp.json()
            if "error_code" in data:
                raise RuntimeError(f"百度OCR错误: {data}")
            return {"text": str(data.get("words_result", "")), "confidence": 0.9, "note": ""}
        except Exception as e:
            logger.warning("百度OCR调用失败(%s)，降级Mock结果", e)
            return {"text": "OCR调用失败降级文本", "confidence": 0.7, "note": str(e)}


# ============================================================
# 阿里云规则引擎（生产环境为云端规则托管，本地兜底复用rule_engine）
# ============================================================
class AliyunRuleEngineAdapter:
    """阿里云规则引擎适配器：未配置时委托本地规则引擎。"""

    def __init__(self):
        self.enabled = False  # 配置云端规则服务后置True

    def evaluate(self, rule_set: str, facts: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if not self.enabled:
            return None  # 返回None表示由调用方降级到本地规则引擎
        try:
            import httpx

            resp = httpx.post(
                f"{settings.PAI_ENDPOINT or 'https://rule.aliyuncs.com'}/evaluate",
                json={"rule_set": rule_set, "facts": facts}, timeout=10,
            )
            return resp.json().get("results")
        except Exception as e:
            logger.warning("阿里云规则引擎调用失败(%s)，降级本地规则引擎", e)
            return None


# ============================================================
# 百融决策引擎
# ============================================================
class BailianDecisionAdapter:
    """百融决策引擎：未配置时返回None（审批决策由本地综合评分完成）。"""

    def __init__(self):
        self.enabled = bool(settings.BAILIAN_API_KEY)

    def decide(self, facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            import httpx

            resp = httpx.post(
                "https://api.100credit.cn/decision/v1",
                headers={"Authorization": f"Bearer {settings.BAILIAN_API_KEY}"},
                json=facts, timeout=10,
            )
            return resp.json()
        except Exception as e:
            logger.warning("百融决策引擎调用失败(%s)，降级本地综合评分", e)
            return None


# ============================================================
# 阿里PAI（机器学习平台模型服务）
# ============================================================
class PaiPlatformAdapter:
    """阿里PAI模型服务：未部署时返回None（降级本地评分卡模型）。"""

    def __init__(self):
        self.enabled = bool(settings.PAI_ENDPOINT)

    def predict_score(self, features: Dict[str, Any]) -> Optional[float]:
        if not self.enabled:
            return None
        try:
            import httpx

            resp = httpx.post(
                f"{settings.PAI_ENDPOINT}/predict",
                json={"features": features}, timeout=10,
            )
            return float(resp.json().get("score"))
        except Exception as e:
            logger.warning("PAI模型调用失败(%s)，降级本地评分卡", e)
            return None


# ============================================================
# Feast特征平台
# ============================================================
class FeastAdapter:
    """Feast特征平台：未配置时基于申请单实时计算特征。"""

    def __init__(self):
        self.enabled = False  # 配置Feast在线服务后置True

    def get_online_features(self, app: LoanApplication) -> Dict[str, Any]:
        """获取在线特征（Mock：由申请单与设备信息实时派生）。"""
        d = app.device
        return {
            "device_age_days": float(d.first_seen_days),
            "phone_registration_months": float(app.applicant.phone_registration_months),
            "account_count": float(d.account_count),
            "apply_channel": app.channel.value,
            "is_repeat_customer": app.loan_request.is_repeat_customer,
        }


# ============================================================
# 单例
# ============================================================
_ocr: Optional[BaiduOCRAdapter] = None
_aliyun_rules: Optional[AliyunRuleEngineAdapter] = None
_bailian: Optional[BailianDecisionAdapter] = None
_pai: Optional[PaiPlatformAdapter] = None
_feast: Optional[FeastAdapter] = None


def get_ocr_adapter() -> BaiduOCRAdapter:
    global _ocr
    if _ocr is None:
        _ocr = BaiduOCRAdapter()
    return _ocr


def get_aliyun_rules() -> AliyunRuleEngineAdapter:
    global _aliyun_rules
    if _aliyun_rules is None:
        _aliyun_rules = AliyunRuleEngineAdapter()
    return _aliyun_rules


def get_bailian() -> BailianDecisionAdapter:
    global _bailian
    if _bailian is None:
        _bailian = BailianDecisionAdapter()
    return _bailian


def get_pai() -> PaiPlatformAdapter:
    global _pai
    if _pai is None:
        _pai = PaiPlatformAdapter()
    return _pai


def get_feast() -> FeastAdapter:
    global _feast
    if _feast is None:
        _feast = FeastAdapter()
    return _feast
