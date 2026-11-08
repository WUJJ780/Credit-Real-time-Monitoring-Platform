"""全局配置：从 .env.local 读取环境变量，未配置时使用安全默认值。"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（credit_risk_system/config/settings.py 向上三级）
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# 加载环境变量（.env.local 优先，其次 .env）
load_dotenv(ROOT_DIR / ".env.local", override=False)
load_dotenv(ROOT_DIR / ".env", override=False)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _get_bool(key: str, default: bool = False) -> bool:
    return _get(key, str(default).lower()).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """系统配置（模块级单例 settings）。"""

    # ---------- LLM ----------
    LLM_BASE_URL: str = _get("LLM_BASE_URL", "https://api.deepseek.com")
    LLM_API_KEY: str = _get("LLM_API_KEY", "")
    LLM_MODEL: str = _get("LLM_MODEL", "deepseek-chat")
    LLM_TEMPERATURE: float = float(_get("LLM_TEMPERATURE", "0.1"))

    # ---------- 数据能力层 ----------
    MYSQL_ENABLED: bool = _get_bool("MYSQL_ENABLED")
    MYSQL_HOST: str = _get("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT: int = int(_get("MYSQL_PORT", "3306"))
    MYSQL_USER: str = _get("MYSQL_USER", "risk")
    MYSQL_PASSWORD: str = _get("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = _get("MYSQL_DATABASE", "credit_risk")

    KAFKA_ENABLED: bool = _get_bool("KAFKA_ENABLED")
    KAFKA_BOOTSTRAP_SERVERS: str = _get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")

    NEO4J_ENABLED: bool = _get_bool("NEO4J_ENABLED")
    NEO4J_URI: str = _get("NEO4J_URI", "bolt://127.0.0.1:7687")
    NEO4J_USER: str = _get("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = _get("NEO4J_PASSWORD", "")

    MILVUS_ENABLED: bool = _get_bool("MILVUS_ENABLED")
    MILVUS_URI: str = _get("MILVUS_URI", "http://127.0.0.1:19530")

    MINIO_ENABLED: bool = _get_bool("MINIO_ENABLED")
    MINIO_ENDPOINT: str = _get("MINIO_ENDPOINT", "127.0.0.1:9000")
    MINIO_ACCESS_KEY: str = _get("MINIO_ACCESS_KEY", "minioadmin")
    MINIO_SECRET_KEY: str = _get("MINIO_SECRET_KEY", "")
    MINIO_BUCKET: str = _get("MINIO_BUCKET", "credit-risk")
    MINIO_SECURE: bool = _get_bool("MINIO_SECURE")

    DORIS_ENABLED: bool = _get_bool("DORIS_ENABLED")
    DORIS_HOST: str = _get("DORIS_HOST", "127.0.0.1")
    DORIS_PORT: int = int(_get("DORIS_PORT", "9030"))

    # ---------- 外部服务 ----------
    BAIDU_OCR_API_KEY: str = _get("BAIDU_OCR_API_KEY", "")
    BAIDU_OCR_SECRET_KEY: str = _get("BAIDU_OCR_SECRET_KEY", "")
    BAILIAN_API_KEY: str = _get("BAILIAN_API_KEY", "")
    PAI_ENDPOINT: str = _get("PAI_ENDPOINT", "")

    # ---------- MCP ----------
    @property
    def MCP_SERVERS(self) -> list:
        raw = _get("MCP_SERVERS", "[]")
        try:
            return json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            return []

    # ---------- 异步高并发 ----------
    # 异步流水线最大同时处理申请数（超限排队）
    ASYNC_MAX_APPLICATIONS: int = int(_get("ASYNC_MAX_APPLICATIONS", "100"))
    # LLM并发调用上限（保护大模型服务）
    LLM_MAX_CONCURRENCY: int = int(_get("LLM_MAX_CONCURRENCY", "10"))
    # 数据能力层（阻塞SDK线程卸载）并发上限
    DATA_MAX_CONCURRENCY: int = int(_get("DATA_MAX_CONCURRENCY", "50"))
    # 单个申请流程的超时时间（秒），超时标记TIMEOUT
    PIPELINE_TIMEOUT_SECONDS: int = int(_get("PIPELINE_TIMEOUT_SECONDS", "300"))
    # MySQL真实模式连接池大小（同步驱动在受控线程池中并发）
    DB_POOL_SIZE: int = int(_get("DB_POOL_SIZE", "10"))
    # 异步API服务
    SERVER_HOST: str = _get("SERVER_HOST", "0.0.0.0")
    SERVER_PORT: int = int(_get("SERVER_PORT", "8000"))

    # ---------- 系统 ----------
    LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")
    DATA_DIR: Path = ROOT_DIR / ".data"
    OUTPUT_DIR: Path = ROOT_DIR / "output"
    LOG_DIR: Path = ROOT_DIR / "logs"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.LLM_API_KEY)

    def ensure_dirs(self) -> None:
        for d in (self.DATA_DIR, self.OUTPUT_DIR, self.LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
