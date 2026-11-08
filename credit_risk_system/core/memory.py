"""MemorySaver管理：LangGraph状态检查点 + 跨申请记忆。

1. get_checkpointer(): LangGraph图编译用的MemorySaver单例，
   以申请单号作为thread_id，支持流程中断恢复与状态审计。
2. AgentMemory: 跨申请的记忆（存量客户识别、历史决策查询）。
"""
import threading
from typing import Dict, Optional

try:
    from langgraph.checkpoint.memory import InMemorySaver
except ImportError:  # 兼容旧版langgraph
    from langgraph.checkpoint.memory import MemorySaver as InMemorySaver

from credit_risk_system.models.risk import FinalDecision

_checkpointer: Optional[InMemorySaver] = None
_lock = threading.Lock()


def get_checkpointer() -> InMemorySaver:
    """获取LangGraph MemorySaver单例（线程安全）。"""
    global _checkpointer
    with _lock:
        if _checkpointer is None:
            _checkpointer = InMemorySaver()
    return _checkpointer


class AgentMemory:
    """跨申请记忆：按申请单号存储最终决策，供贷后/复贷查询。"""

    def __init__(self):
        self._decisions: Dict[str, FinalDecision] = {}
        self._lock = threading.Lock()

    def save(self, decision: FinalDecision) -> None:
        with self._lock:
            self._decisions[decision.application_id] = decision

    def get(self, application_id: str) -> Optional[FinalDecision]:
        with self._lock:
            return self._decisions.get(application_id)

    def all(self) -> Dict[str, FinalDecision]:
        with self._lock:
            return dict(self._decisions)


_memory: Optional[AgentMemory] = None


def get_agent_memory() -> AgentMemory:
    global _memory
    if _memory is None:
        _memory = AgentMemory()
    return _memory
