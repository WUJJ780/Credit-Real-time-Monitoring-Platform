"""兼容层：Agent结构化输出模型的旧导入路径。

⚠️ 原模型定义已迁移至「单一事实源」：
    credit_risk_system/agents/resume/state.py

本模块保留原路径可用（与 resume/state.py 为同一批类对象）：
    from credit_risk_system.models.risk import AgentResult, FinalDecision, ...

新代码请直接使用：
    from credit_risk_system.agents.resume.state import AgentResult, ...

实现说明：采用模块级 __getattr__（PEP 562）惰性转发而非顶部re-export，
以规避导入顺序循环——models.risk → agents.resume.state 会触发
agents包 __init__ → base_agent → models.risk 的链式依赖。
"""
from typing import Any

__all__ = [
    # 通用枚举
    "AgentStatus", "NextAction", "FraudLevel", "CreditGrade",
    "RiskLevel", "ApprovalDecision", "FinalStatus",
    # 通用基础
    "AgentResult", "AuditEvent",
    # 进件审核Agent
    "IntakeResult",
    # 反欺诈Agent
    "BlacklistHit", "MultiLoanAssessment", "DeviceRiskAssessment",
    "GangDetection", "SimilarCase", "AntiFraudResult",
    # 信用评估Agent
    "CreditReportSummary", "RepaymentAbility", "ScoreCardResult", "CreditResult",
    # 审批决策Agent
    "ApprovalResult",
    # 合规审计Agent
    "ComplianceIssue", "ComplianceResult",
    # 贷后管理Agent
    "PostLoanResult",
    # 风控中台Agent与流程上下文
    "FinalDecision", "PipelineContext",
]


def __getattr__(name: str) -> Any:
    """惰性转发：所有符号解析到 resume/state.py 的同名定义。"""
    from credit_risk_system.agents.resume import state as _state

    return getattr(_state, name)
