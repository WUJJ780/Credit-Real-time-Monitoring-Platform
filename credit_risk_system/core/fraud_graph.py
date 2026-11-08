"""反欺诈知识图谱：基于Neo4j的团伙欺诈识别，离线时降级为内存图。

关系类型：SHARED_DEVICE(共享设备) SHARED_PHONE(共享手机号) SHARED_IP(共享IP)
          SAME_EMPLOYER(同单位) INVOLVED(涉及案件)
团伙识别：从申请人出发广度遍历可疑关系（设备/手机/IP），若连通组件中
包含已确认欺诈团伙成员或历史欺诈案件节点，判定为团伙欺诈风险。
"""
import threading
from abc import ABC, abstractmethod
from collections import deque
from typing import Dict, List, Optional, Tuple

from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger
from credit_risk_system.models.loan import LoanApplication
from credit_risk_system.models.risk import GangDetection

SUSPICIOUS_RELATIONS = ("SHARED_DEVICE", "SHARED_PHONE", "SHARED_IP")
logger = get_logger("fraud_graph")


class FraudGraph(ABC):
    """知识图谱抽象接口。"""

    @abstractmethod
    def add_application(self, app: LoanApplication) -> None:
        """将申请单写入图谱并建立关联边。"""

    @abstractmethod
    def detect_gangs(self, application_id: str, depth: int = 2) -> GangDetection:
        """团伙欺诈检测。"""


class InMemoryFraudGraph(FraudGraph):
    """内存实现（离线演示/单元测试）。线程安全：高并发下多任务并发读写。"""

    def __init__(self):
        self.nodes: Dict[str, dict] = {}   # node_id -> {type, label, ...}
        self.edges: Dict[str, List[Tuple[str, str]]] = {}  # node_id -> [(neighbor, relation)]
        self._lock = threading.RLock()

    # ---------------- 写入 ----------------
    def add_application(self, app: LoanApplication) -> None:
        node_id = app.application_id
        attrs = {
            "type": "applicant",
            "label": "normal",
            "device_id": app.device.device_id,
            "phone": app.applicant.phone,
            "ip": app.device.ip,
            "employer": app.applicant.employer,
        }
        with self._lock:
            if node_id in self.nodes:
                return  # 幂等
            # 与历史节点（含欺诈团伙/案件节点）建立可疑关联边
            links: List[Tuple[str, str]] = []
            for other, other_attrs in self.nodes.items():
                if attrs["device_id"] and attrs["device_id"] == other_attrs.get("device_id"):
                    links.append((other, "SHARED_DEVICE"))
                if attrs["phone"] and attrs["phone"] == other_attrs.get("phone"):
                    links.append((other, "SHARED_PHONE"))
                if attrs["ip"] and attrs["ip"] == other_attrs.get("ip"):
                    links.append((other, "SHARED_IP"))
                if (other_attrs.get("type") == "applicant"
                        and attrs["employer"] and attrs["employer"] == other_attrs.get("employer")):
                    links.append((other, "SAME_EMPLOYER"))
            self.nodes[node_id] = attrs
            self.edges[node_id] = links
            for other, rel in links:
                self.edges.setdefault(other, []).append((node_id, rel))
        logger.debug("图谱写入节点: %s, 关联边: %d条", node_id, len(links))

    def add_fraud_node(self, node_id: str, node_type: str, **attrs) -> None:
        """写入欺诈团伙成员/欺诈案件节点（供种子数据）。"""
        with self._lock:
            if node_id in self.nodes:
                return
            attrs = {"type": node_type, "label": "fraud", **attrs}
            self.nodes[node_id] = attrs
            self.edges.setdefault(node_id, [])

    def link(self, a: str, b: str, relation: str) -> None:
        with self._lock:
            if b not in self.nodes:
                return
            self.edges.setdefault(a, []).append((b, relation))
            self.edges.setdefault(b, []).append((a, relation))

    # ---------------- 查询 ----------------
    def _bfs(self, start: str, depth: int) -> List[Tuple[str, str, int]]:
        """广度遍历可疑关系，返回 (节点, 关系, 层数)。"""
        with self._lock:
            seen = {start}
            queue = deque([(start, "", 0)])
            result: List[Tuple[str, str, int]] = []
            while queue:
                node, rel, level = queue.popleft()
                if level > depth:
                    continue
                for neighbor, relation in self.edges.get(node, []):
                    if relation not in SUSPICIOUS_RELATIONS and node != start:
                        continue
                    if neighbor not in seen:
                        seen.add(neighbor)
                        queue.append((neighbor, relation, level + 1))
                        result.append((neighbor, relation, level + 1))
            return result

    def detect_gangs(self, application_id: str, depth: int = 2) -> GangDetection:
        neighbors = self._bfs(application_id, depth)
        if not neighbors:
            return GangDetection()
        component_size = len(neighbors) + 1
        fraud_linked = False
        linked_case: Optional[str] = None
        relation = neighbors[0][1]
        for nid, _, _ in neighbors:
            attrs = self.nodes.get(nid, {})
            if attrs.get("label") == "fraud":
                fraud_linked = True
                if attrs.get("type") == "fraud_case":
                    linked_case = nid
        if fraud_linked:
            score = float(rs.GANG_FRAUD_LINK_SCORE)
            gang_id = f"GANG-{linked_case or 'UNKNOWN'}"
        else:
            score = min(30.0, component_size * 3.0)
            gang_id = f"GROUP-{component_size}" if component_size > 1 else None
        return GangDetection(
            gang_id=gang_id if component_size > 1 else None,
            component_size=component_size,
            relation=relation,
            linked_fraud_case=linked_case,
            score=score,
        )


class Neo4jFraudGraph(FraudGraph):
    """Neo4j实现（生产环境）。"""

    def __init__(self):
        from neo4j import GraphDatabase  # 延迟导入

        self._driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    def add_application(self, app: LoanApplication) -> None:
        query = """
        MERGE (a:Applicant {id: $id})
        SET a.name = $name, a.phone = $phone, a.device_id = $device_id, a.ip = $ip
        WITH a
        MATCH (o:Applicant) WHERE o.id <> $id
          AND (o.device_id = $device_id OR o.phone = $phone OR o.ip = $ip)
        FOREACH (x IN CASE WHEN o.device_id = $device_id THEN [o] ELSE [] END |
            MERGE (a)-[:SHARED_DEVICE]->(x))
        """
        with self._driver.session() as session:
            session.run(
                query,
                id=app.application_id,
                name=app.applicant.name,
                phone=app.applicant.phone,
                device_id=app.device.device_id,
                ip=app.device.ip,
            )

    def detect_gangs(self, application_id: str, depth: int = 2) -> GangDetection:
        query = """
        MATCH (a:Applicant {id: $id})-[*1..2]-(n)
        WHERE n:FraudRing OR n:FraudCase
        WITH collect(DISTINCT n) AS frauds, count(DISTINCT n) AS cnt
        OPTIONAL MATCH (a:Applicant {id: $id})-[*1..2]-(all)
        RETURN cnt AS fraud_cnt, count(DISTINCT all) + 1 AS comp_size,
               [x IN frauds WHERE x:FraudCase | x.id] AS cases
        """
        with self._driver.session() as session:
            record = session.run(query, id=application_id).single()
            if not record:
                return GangDetection()
            fraud_cnt = record["fraud_cnt"] or 0
            comp_size = record["comp_size"] or 1
            cases = record["cases"] or []
            if fraud_cnt > 0:
                return GangDetection(
                    gang_id=f"GANG-{cases[0] if cases else 'UNKNOWN'}",
                    component_size=comp_size,
                    relation="SHARED_DEVICE",
                    linked_fraud_case=cases[0] if cases else None,
                    score=float(rs.GANG_FRAUD_LINK_SCORE),
                )
            return GangDetection(
                component_size=comp_size,
                relation="SHARED_DEVICE",
                score=min(30.0, comp_size * 3.0) if comp_size > 1 else 0.0,
            )


_graph: Optional[FraudGraph] = None


def get_fraud_graph() -> FraudGraph:
    """获取知识图谱单例（Neo4j可用时优先，否则内存图）。"""
    global _graph
    if _graph is None:
        if settings.NEO4J_ENABLED and settings.NEO4J_URI:
            try:
                _graph = Neo4jFraudGraph()
                logger.info("反欺诈知识图谱: Neo4j (%s)", settings.NEO4J_URI)
            except Exception as e:
                logger.warning("Neo4j连接失败(%s)，降级为内存图谱", e)
                _graph = InMemoryFraudGraph()
        else:
            _graph = InMemoryFraudGraph()
            logger.info("反欺诈知识图谱: 内存模式（NEO4J_ENABLED=false）")
        _seed_demo_data(_graph)
    return _graph


def _seed_demo_data(graph: FraudGraph) -> None:
    """写入演示用欺诈团伙种子数据（生产环境替换为历史案件数据）。"""
    if not isinstance(graph, InMemoryFraudGraph):
        return
    ring = graph
    # 已确认欺诈团伙（共享设备/手机号）
    for i in range(1, 6):
        ring.add_fraud_node(f"RING-2025-0{i}", "fraud_ring",
                            device_id="DEV_FRAUD_X", phone=f"1990000{i:04d}")
    # 历史欺诈案件
    ring.add_fraud_node("FRAUD-2025-009", "fraud_case", device_id="DEV_FRAUD_X")
    for i in range(1, 6):
        ring.link("FRAUD-2025-009", f"RING-2025-0{i}", "INVOLVED")
    # 历史正常申请人（用于演示共享设备弱关联）
    ring.add_fraud_node("HIST-NORMAL-001", "applicant",
                        device_id="DEV_0007", label="normal", phone="13600009999")
    logger.debug("已写入演示种子数据: 欺诈团伙RING-2025-0* + 案件FRAUD-2025-009")
