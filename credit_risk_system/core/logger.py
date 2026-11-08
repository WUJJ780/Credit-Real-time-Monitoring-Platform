"""统一日志：控制台 + 文件双输出，UTF-8编码。"""
import logging
import sys

from credit_risk_system.config.settings import settings

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings.ensure_dirs()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s", datefmt="%H:%M:%S"
    )
    # 控制台输出（兼容Windows GBK控制台）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    # 文件输出（审计留痕）
    file_handler = logging.FileHandler(
        settings.LOG_DIR / "credit_risk.log", encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger("credit_risk_system")
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    root.addHandler(stream)
    root.addHandler(file_handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取统一命名空间的logger，如 get_logger('intake_agent')。"""
    _configure()
    return logging.getLogger(f"credit_risk_system.{name}")
