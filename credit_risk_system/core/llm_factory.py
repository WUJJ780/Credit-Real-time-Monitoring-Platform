"""LLM工厂：统一创建OpenAI兼容协议的大模型客户端。

支持 DeepSeek / Qwen / OpenAI 等任意OpenAI兼容服务，
通过 .env.local 中 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 配置。
未配置API Key时返回 None，Agent自动降级到规则引擎层。

高并发异步模式：get_llm_semaphore() 返回LLM调用并发信号量
（上限LLM_MAX_CONCURRENCY），异步流水线中所有ainvoke持有该信号量，
保护大模型服务不被高并发打爆。
"""
import threading
from typing import Optional

from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger

try:
    from langchain_openai import ChatOpenAI
except ImportError:  # 未安装langchain-openai时降级
    ChatOpenAI = None

logger = get_logger("llm_factory")
_lock = threading.Lock()
_llm_instance = None
_llm_inited = False


def get_llm(force_reload: bool = False) -> Optional["ChatOpenAI"]:
    """获取LLM客户端（单例）。未配置API Key时返回 None。"""
    global _llm_instance, _llm_inited
    with _lock:
        if _llm_inited and not force_reload:
            return _llm_instance
        _llm_inited = True
        if not settings.LLM_API_KEY:
            logger.info("未配置LLM_API_KEY，Agent将以「规则引擎」层决策（离线模式）")
            _llm_instance = None
            return None
        if ChatOpenAI is None:
            logger.warning("未安装langchain-openai，无法创建LLM客户端")
            _llm_instance = None
            return None
        try:
            kwargs = {
                "model": settings.LLM_MODEL,
                "api_key": settings.LLM_API_KEY,
                "temperature": settings.LLM_TEMPERATURE,
                "timeout": 60,
                "max_retries": 1,
            }
            if settings.LLM_BASE_URL:
                kwargs["base_url"] = settings.LLM_BASE_URL
            _llm_instance = ChatOpenAI(**kwargs)
            logger.info("LLM客户端初始化完成: model=%s base_url=%s",
                        settings.LLM_MODEL, settings.LLM_BASE_URL or "(默认)")
        except Exception as e:  # 配置错误不阻塞主流程
            logger.error("LLM客户端初始化失败: %s", e)
            _llm_instance = None
        return _llm_instance


def get_llm_semaphore():
    """获取LLM调用并发信号量（当前事件循环，惰性创建）。"""
    from credit_risk_system.core.async_utils import get_llm_semaphore as _get

    return _get()
