"""三层兜底重试机制。

每个Agent的决策按以下顺序执行，任一层成功即返回：
  第1层 LLM推理   —— 大模型结构化推理（需配置LLM_API_KEY）
  第2层 规则引擎  —— 本地确定性规则引擎（离线可用的主决策路径）
  第3层 默认兜底  —— 保守默认策略（宁可转人工，不可误放行）

层间切换会记录审计事件，供合规审计与监控告警使用。
"""
import asyncio
import time
from typing import Any, Callable, List, Sequence, Tuple

from credit_risk_system.core.exceptions import FallbackExhaustedError
from credit_risk_system.core.logger import get_logger


class FallbackOutcome:
    """兜底执行结果：value=决策结果, layer=实际生效层。"""

    __slots__ = ("value", "layer")

    def __init__(self, value: Any, layer: str):
        self.value = value
        self.layer = layer


def retry_call(
    fn: Callable[[], Any],
    *,
    retries: int = 2,
    base_delay: float = 0.3,
    exceptions: Tuple[type, ...] = (Exception,),
    context: str = "",
) -> Any:
    """指数退避重试（处理瞬时故障，如LLM超时）。"""
    logger = get_logger("retry")
    last: Exception = Exception("unknown")
    for attempt in range(retries + 1):
        try:
            return fn()
        except exceptions as e:  # noqa: PERF203
            last = e
            if attempt < retries:
                delay = base_delay * (2 ** attempt)
                logger.warning("[%s] 第%d次调用失败(%s)，%.1fs后重试", context, attempt + 1, e, delay)
                time.sleep(delay)
    raise last


def run_with_fallback(
    layers: Sequence[Tuple[str, Callable[[], Any]]],
    *,
    context: str = "",
    logger=None,
) -> FallbackOutcome:
    """按序执行兜底层，返回 (结果, 生效层名)；全部失败则抛出。"""
    logger = logger or get_logger("fallback")
    last: Exception = Exception("unknown")
    total = len(layers)
    for idx, (name, fn) in enumerate(layers, start=1):
        try:
            value = fn()
            if idx > 1:
                logger.warning(
                    "[%s] 前%d层失败，当前由第%d层「%s」兜底决策",
                    context, idx - 1, idx, name,
                )
            return FallbackOutcome(value, name)
        except Exception as e:  # 捕获一切异常并降级
            last = e
            logger.warning("[%s] 第%d层「%s」失败: %s → 降级到下一层", context, idx, name, e)
    raise FallbackExhaustedError(context, last)


async def arun_with_fallback(
    layers: Sequence[Tuple[str, Callable[[], Any]]],
    *,
    context: str = "",
    logger=None,
) -> FallbackOutcome:
    """异步版三层兜底：层函数可为协程（LLM层）或同步函数（规则层）。

    同步函数层由调用方通过 asyncio.to_thread 包装后传入即可，
    此处统一 await 处理，保证事件循环不被阻塞。
    """
    logger = logger or get_logger("fallback")
    last: Exception = Exception("unknown")
    for idx, (name, fn) in enumerate(layers, start=1):
        try:
            value = fn()
            if asyncio.iscoroutine(value):
                value = await value
            if idx > 1:
                logger.warning(
                    "[%s] 前%d层失败，当前由第%d层「%s」兜底决策",
                    context, idx - 1, idx, name,
                )
            return FallbackOutcome(value, name)
        except Exception as e:  # 捕获一切异常并降级
            last = e
            logger.warning("[%s] 第%d层「%s」失败: %s → 降级到下一层", context, idx, name, e)
    raise FallbackExhaustedError(context, last)


def three_layer(name: str, secondary: Callable, tertiary: Callable):
    """三层兜底装饰器：LLM主决策 -> 规则引擎 -> 默认兜底。

    :param name: Agent名称（用于日志与审计）
    :param secondary: 规则引擎决策函数，签名 fn(ctx, evidence) -> AgentResult
    :param tertiary: 默认兜底函数，签名 fn(ctx, evidence) -> AgentResult
    """
    def decorator(primary: Callable):
        def wrapper(self, ctx, evidence):
            outcome = run_with_fallback(
                [
                    ("LLM推理", lambda: primary(self, ctx, evidence)),
                    ("规则引擎", lambda: secondary(self, ctx, evidence)),
                    ("默认兜底", lambda: tertiary(self, ctx, evidence)),
                ],
                context=name,
                logger=get_logger(name),
            )
            return outcome
        return wrapper
    return decorator
