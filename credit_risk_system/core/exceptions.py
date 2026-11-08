"""异常体系：全系统统一异常层次，便于监控告警与异常升级。"""


class CreditRiskError(Exception):
    """系统根异常。"""
    code = "CRS000"


class LLMError(CreditRiskError):
    """LLM调用异常（触发三层兜底降级）。"""
    code = "CRS100"


class LLMUnavailableError(LLMError):
    """LLM未配置/不可用。"""
    code = "CRS101"


class LLMTimeoutError(LLMError):
    """LLM调用超时。"""
    code = "CRS102"


class AgentExecutionError(CreditRiskError):
    """Agent执行异常。"""
    code = "CRS200"


class DataSourceError(CreditRiskError):
    """数据源访问异常。"""
    code = "CRS300"


class RuleViolationError(CreditRiskError):
    """风控规则硬性拒绝。"""
    code = "CRS400"

    def __init__(self, rule_name: str, message: str = ""):
        self.rule_name = rule_name
        super().__init__(message or f"命中硬性风控规则: {rule_name}")


class ComplianceError(CreditRiskError):
    """合规违规（合规一票否决）。"""
    code = "CRS500"


class OrchestrationError(CreditRiskError):
    """流程编排异常。"""
    code = "CRS600"


class MessageBusError(CreditRiskError):
    """消息总线异常。"""
    code = "CRS700"


class FallbackExhaustedError(CreditRiskError):
    """三层兜底全部失败。"""
    code = "CRS800"

    def __init__(self, context: str, cause: Exception):
        self.context = context
        self.cause = cause
        super().__init__(f"[{context}] 三层兜底(LLM/规则/默认)全部失败: {cause}")
