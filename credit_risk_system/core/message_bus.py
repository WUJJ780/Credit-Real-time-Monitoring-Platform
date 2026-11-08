"""消息总线：Agent间通讯协议（Kafka），离线时降级为内存总线。

主题约定：
  risk.<agent_name>  各Agent环节结果事件
  risk.decision      最终决策事件
  risk.alerts        告警升级事件（自动拒绝/人工复核/异常）
"""
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

from credit_risk_system.agents.resume.state import EventEnvelope
from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger

logger = get_logger("message_bus")

# EventEnvelope 已迁移至 resume/state.py（单一事实源），此处 re-export
# 保持 from credit_risk_system.core.message_bus import EventEnvelope 可用。
__all__ = ["EventEnvelope", "MessageBus", "InMemoryBus", "KafkaBus", "get_message_bus"]


class MessageBus(ABC):
    @abstractmethod
    def publish(self, envelope: EventEnvelope) -> bool: ...

    @abstractmethod
    def subscribe(self, topic: str, handler: Callable[[EventEnvelope], None]) -> None: ...

    async def apublish(self, envelope: EventEnvelope) -> bool:
        """异步发布（默认卸载到线程池，避免阻塞SDK卡住事件循环）。"""
        import asyncio

        return await asyncio.to_thread(self.publish, envelope)


class InMemoryBus(MessageBus):
    """内存总线（离线演示/测试）。线程安全：高并发下多任务同时发布。"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self.history: List[EventEnvelope] = []
        self._lock = threading.Lock()

    def publish(self, envelope: EventEnvelope) -> bool:
        with self._lock:
            self.history.append(envelope)
            handlers = list(self._subscribers.get(envelope.topic, []))
        for handler in handlers:
            try:
                handler(envelope)
            except Exception as e:
                logger.warning("订阅者处理事件失败: %s", e)
        logger.debug("事件发布: topic=%s type=%s app=%s",
                     envelope.topic, envelope.event_type, envelope.application_id)
        return True

    async def apublish(self, envelope: EventEnvelope) -> bool:
        # 内存操作无需线程卸载，直接同步发布（内部已加锁）
        return self.publish(envelope)

    def subscribe(self, topic: str, handler: Callable[[EventEnvelope], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(topic, []).append(handler)


class KafkaBus(MessageBus):
    """Kafka总线（生产环境，kafka-python）。"""

    def __init__(self, bootstrap_servers: str):
        from kafka import KafkaProducer  # 延迟导入
        from kafka.errors import NoBrokersAvailable

        self.bootstrap = bootstrap_servers
        try:
            self._producer = KafkaProducer(
                bootstrap_servers=bootstrap_servers.split(","),
                value_serializer=lambda v: v.encode("utf-8"),
                acks=1,
            )
            self._available = True
        except NoBrokersAvailable as e:
            logger.warning("Kafka不可用(%s)，事件发布降级为日志留痕", e)
            self._producer = None
            self._available = False
        self._subscribers: Dict[str, List[Callable]] = {}

    def publish(self, envelope: EventEnvelope) -> bool:
        if not self._available:
            return False
        try:
            self._producer.send(
                envelope.topic, envelope.model_dump_json()
            ).get(timeout=3)
            return True
        except Exception as e:
            logger.warning("Kafka发布失败: %s", e)
            return False

    def subscribe(self, topic: str, handler: Callable[[EventEnvelope], None]) -> None:
        """启动消费线程（daemon），新事件到达时回调handler。"""
        import json
        import threading

        from kafka import KafkaConsumer

        def consume():
            consumer = KafkaConsumer(
                topic,
                bootstrap_servers=self.bootstrap.split(","),
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                auto_offset_reset="latest",
                group_id="credit-risk-system",
            )
            for msg in consumer:
                try:
                    handler(EventEnvelope(**msg.value))
                except Exception as e:
                    logger.warning("消费事件处理失败: %s", e)

        threading.Thread(target=consume, daemon=True, name=f"kafka-{topic}").start()


_bus: Optional[MessageBus] = None


def get_message_bus() -> MessageBus:
    """获取消息总线单例。"""
    global _bus
    if _bus is None:
        if settings.KAFKA_ENABLED:
            _bus = KafkaBus(settings.KAFKA_BOOTSTRAP_SERVERS)
            logger.info("消息总线: Kafka (%s)", settings.KAFKA_BOOTSTRAP_SERVERS)
        else:
            _bus = InMemoryBus()
            logger.info("消息总线: 内存模式（KAFKA_ENABLED=false）")
    return _bus
