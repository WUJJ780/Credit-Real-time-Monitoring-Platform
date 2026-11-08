"""异步并发控制工具：信号量注册表 + 阻塞调用卸载。

高并发异步模式的核心约定：
  - LLM 层     : 原生异步（ainvoke），受 LLM_MAX_CONCURRENCY 信号量限流；
  - 数据能力层 : 阻塞SDK（pymysql/kafka/minio/milvus等）统一通过 run_blocking()
                 卸载到线程池，受 DATA_MAX_CONCURRENCY 信号量限流，
                 避免阻塞事件循环、避免无界创建线程；
  - 规则引擎等 : 纯CPU计算，直接在事件循环内执行（耗时微秒级）。

信号量按「事件循环」惰性创建并缓存：同一进程内不同事件循环
（如uvicorn reload重启）各自持有独立信号量，避免跨循环绑定错误。
"""
import asyncio
from typing import Any, Callable, Dict, Optional, Tuple

from credit_risk_system.config.settings import settings

# key -> (loop, Semaphore)
_loop_semaphores: Dict[str, Tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}


def get_semaphore(key: str, limit: int) -> Optional[asyncio.Semaphore]:
    """获取当前事件循环的信号量（无运行中事件循环时返回None）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    cached = _loop_semaphores.get(key)
    if cached is None or cached[0] is not loop:
        _loop_semaphores[key] = (loop, asyncio.Semaphore(max(1, limit)))
    return _loop_semaphores[key][1]


def get_data_semaphore() -> Optional[asyncio.Semaphore]:
    """数据能力层并发信号量（限流阻塞SDK线程卸载）。"""
    return get_semaphore("data", settings.DATA_MAX_CONCURRENCY)


def get_llm_semaphore() -> Optional[asyncio.Semaphore]:
    """LLM调用并发信号量（保护大模型服务不被打爆）。"""
    return get_semaphore("llm", settings.LLM_MAX_CONCURRENCY)


async def run_blocking(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """将阻塞调用卸载到线程池执行（受数据层并发信号量限流）。

    不在事件循环内（如CLI同步路径）时直接同步调用。
    """
    sem = get_data_semaphore()
    if sem is None:
        return fn(*args, **kwargs)
    async with sem:
        return await asyncio.to_thread(fn, *args, **kwargs)
