"""resume —— 流程状态与中断恢复层。

集中定义多Agent系统的全部「结构化输出模型」与「状态字典」
（见 state.py）：
  - 各风控Agent的结构化输出模型（Pydantic v2 BaseModel）
  - LangGraph流程状态字典 WorkflowState（TypedDict）
  - 异步流水线状态（受理状态枚举 / 统计 / 查询与事件载荷）
  - 消息总线事件信封 EventEnvelope

设计动机：LangGraph检查点（thread_id=申请单号）的中断恢复(resume)
要求状态模型集中、稳定、可追溯；所有新增Agent输出须在此登记。

用法：
    from credit_risk_system.agents.resume.state import (
        AgentResult, WorkflowState, FinalDecision, ...
    )
"""
