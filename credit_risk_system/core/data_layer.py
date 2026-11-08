"""数据能力层适配器：MySQL / Milvus / MinIO / Doris。

每个存储组件支持两种模式：
  - 真实模式：对应 *_ENABLED=true 时连接真实基础设施（docker-compose提供）
  - Mock模式：内存/本地文件实现，内置演示数据，保证离线可运行
未安装对应SDK时自动降级为Mock（生产环境请在requirements.txt安装全部依赖）。

高并发说明：真实模式的同步驱动（pymysql/minio/pymilvus等）由异步流水线
通过线程池卸载调用（见core/async_utils.run_blocking），因此本模块内部
必须线程安全：
  - MySQL真实模式使用连接池（DB_POOL_SIZE），不再共享单连接；
  - Mock内存字典写入加锁保护。
"""
import queue
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger
from credit_risk_system.models.loan import LoanApplication
from credit_risk_system.models.risk import FinalDecision

logger = get_logger("data_layer")

# ============================================================
# 演示用Mock数据（与main.py演示案例对应）
# ============================================================
# 三要素核验注册表: (姓名, 身份证号, 手机号)
MOCK_IDENTITIES = {
    ("王小明", "110101199503123456", "13800000001"): True,
    ("陈强", "440305199003084321", "13700000002"): True,
    ("张丽", "310104198201234567", "13600000003"): True,
    ("刘洋", "110101199701015678", "13500000004"): True,
    ("王强", "320102199207084321", "13400000005"): True,
}

# 黑名单库: 按身份证号
MOCK_BLACKLIST = {
    "440305199003084321": {
        "list_type": "失信", "source": "最高法失信被执行人名单",
        "matched_on": "身份证号", "level": "SEVERE",
        "detail": "因债务纠纷被列入失信被执行人名单",
    },
}

# 多头借贷记录: 按身份证号
MOCK_MULTILOAN = {
    "110101199503123456": {"platform_count": 2, "query_7d": 0, "total_balance": 60000,
                           "platforms": ["借呗", "招联金融"]},
    "440305199003084321": {"platform_count": 12, "query_7d": 9, "total_balance": 380000,
                           "platforms": ["某网贷A", "某网贷B", "某网贷C", "某网贷D", "某网贷E",
                                         "某网贷F", "某网贷G", "某网贷H", "某网贷I", "某网贷J",
                                         "某网贷K", "某网贷L"]},
    "310104198201234567": {"platform_count": 6, "query_7d": 4, "total_balance": 220000,
                           "platforms": ["微粒贷", "京东金条", "美团借钱", "借呗", "度小满", "某网贷"]},
    "320102199207084321": {"platform_count": 3, "query_7d": 0, "total_balance": 45000,
                           "platforms": ["借呗", "招联金融", "度小满"]},
}

# 征信报告摘要: 按身份证号
MOCK_CREDIT_REPORTS = {
    "110101199503123456": {"has_report": True, "first_loan_years": 8.0,
                           "overdue_m1": 0, "overdue_m2": 0, "overdue_m3": 0,
                           "overdue_amount": 0.0, "total_limit": 170000.0, "used_limit": 59500.0,
                           "utilization": 0.35, "query_count_3m": 2, "credit_cards": 3,
                           "special_records": []},
    "310104198201234567": {"has_report": True, "first_loan_years": 2.0,
                           "overdue_m1": 1, "overdue_m2": 0, "overdue_m3": 0,
                           "overdue_amount": 1200.0, "total_limit": 150000.0, "used_limit": 82500.0,
                           "utilization": 0.55, "query_count_3m": 6, "credit_cards": 5,
                           "special_records": ["近6个月有1次信用卡最低还款记录"]},
    "320102199207084321": {"has_report": True, "first_loan_years": 5.0,
                           "overdue_m1": 0, "overdue_m2": 0, "overdue_m3": 0,
                           "overdue_amount": 0.0, "total_limit": 200000.0, "used_limit": 40000.0,
                           "utilization": 0.20, "query_count_3m": 2, "credit_cards": 2,
                           "special_records": []},
}

_DEFAULT_REPORT = {"has_report": True, "first_loan_years": 3.0,
                   "overdue_m1": 0, "overdue_m2": 0, "overdue_m3": 0,
                   "overdue_amount": 0.0, "total_limit": 50000.0, "used_limit": 15000.0,
                   "utilization": 0.30, "query_count_3m": 1, "credit_cards": 1,
                   "special_records": []}


# ============================================================
# MySQL适配器（黑名单/多头/征信/进件/决策）
# ============================================================
class MySQLStore:
    """MySQL数据访问（Mock模式内置演示数据）。

    真实模式使用连接池（DB_POOL_SIZE）而非共享单连接：
    高并发异步流水线通过线程池并发调用本类方法时，各线程从池中取用
    独立连接，避免pymysql连接非线程安全导致的串扰。
    """

    def __init__(self):
        self._mock = True
        self._pool: queue.Queue = queue.Queue()
        self._mock_lock = threading.Lock()
        self._applications: Dict[str, dict] = {}
        self._decisions: Dict[str, dict] = {}
        if settings.MYSQL_ENABLED:
            try:
                import pymysql  # 延迟导入

                pool = queue.Queue(maxsize=max(1, settings.DB_POOL_SIZE))
                for _ in range(max(1, settings.DB_POOL_SIZE)):
                    pool.put(pymysql.connect(
                        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
                        user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD,
                        database=settings.MYSQL_DATABASE, charset="utf8mb4",
                    ))
                self._pool = pool
                self._mock = False
            except Exception as e:
                logger.warning("MySQL连接失败(%s)，降级为Mock模式", e)

    @contextmanager
    def _conn(self):
        """从连接池借出一个连接，用毕归还。"""
        conn = self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put(conn)

    # ---------- 读取 ----------
    def verify_identity(self, name: str, id_number: str, phone: str) -> bool:
        """三要素核验。"""
        if self._mock:
            return MOCK_IDENTITIES.get((name, id_number, phone), False)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM t_identity WHERE name=%s AND id_number=%s AND phone=%s",
                (name, id_number, phone),
            )
            return cur.fetchone() is not None

    def get_blacklist(self, id_number: str, phone: str = "") -> List[dict]:
        """黑名单匹配（司法/失信/行内/同业）。"""
        if self._mock:
            hit = MOCK_BLACKLIST.get(id_number)
            return [hit] if hit else []
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT list_type, source, matched_on, level, detail FROM t_blacklist "
                "WHERE id_number=%s OR phone=%s", (id_number, phone),
            )
            cols = ("list_type", "source", "matched_on", "level", "detail")
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_multi_loan(self, id_number: str) -> Dict[str, Any]:
        """多头借贷数据。"""
        if self._mock:
            return dict(MOCK_MULTILOAN.get(id_number,
                        {"platform_count": 0, "query_7d": 0, "total_balance": 0, "platforms": []}))
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT platform_count, query_7d, total_balance, platforms "
                        "FROM t_multi_loan WHERE id_number=%s", (id_number,))
            row = cur.fetchone()
            if not row:
                return {"platform_count": 0, "query_7d": 0, "total_balance": 0, "platforms": []}
            return {"platform_count": row[0], "query_7d": row[1],
                    "total_balance": row[2], "platforms": row[3].split(";")}

    def get_credit_report(self, id_number: str) -> Dict[str, Any]:
        """征信报告摘要。"""
        if self._mock:
            return dict(MOCK_CREDIT_REPORTS.get(id_number, _DEFAULT_REPORT))
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM t_credit_report WHERE id_number=%s", (id_number,))
            row = cur.fetchone()
            return dict(zip(("has_report", "first_loan_years", "overdue_m1", "overdue_m2",
                             "overdue_m3", "overdue_amount", "total_limit", "used_limit",
                             "utilization", "query_count_3m", "credit_cards", "special_records"),
                            row)) if row else dict(_DEFAULT_REPORT)

    # ---------- 写入 ----------
    def save_application(self, app: LoanApplication) -> None:
        if self._mock:
            with self._mock_lock:
                self._applications[app.application_id] = app.model_dump(mode="json")
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_application(application_id, applicant_name, id_number, amount, "
                "term, purpose, channel, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (app.application_id, app.applicant.name, app.applicant.id_number,
                 app.loan_request.amount, app.loan_request.term_months,
                 app.loan_request.purpose.value, app.channel.value, app.apply_time),
            )
        conn.commit()

    def save_decision(self, decision: FinalDecision) -> None:
        if self._mock:
            with self._mock_lock:
                self._decisions[decision.application_id] = decision.model_dump(mode="json")
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_decision(application_id, decision, final_amount, final_rate, "
                "audit_ref, decided_at) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE decision=VALUES(decision)",
                (decision.application_id, decision.decision.value, decision.final_amount,
                 decision.final_rate, decision.audit_ref, decision.decided_at),
            )
        conn.commit()


# ============================================================
# Milvus适配器（历史案件向量相似检索）
# ============================================================
# 历史案件库（Mock）：特征向量手工构造，生产环境替换为真实案件embedding
_MILVUS_FEATURE_KEYS = ("multi_platforms", "device_days", "income", "debt_ratio",
                        "overdue_total", "query_3m", "gang_links", "device_risk")
_MILVUS_SCALES = {"multi_platforms": 15.0, "device_days": 720.0, "income": 50000.0,
                  "debt_ratio": 1.0, "overdue_total": 10.0, "query_3m": 12.0,
                  "gang_links": 6.0, "device_risk": 100.0}
_MILVUS_HISTORICAL = [
    {"case_id": "FRAUD-2025-009", "label": "fraud",
     "features": {"multi_platforms": 14, "device_days": 2, "income": 20000, "debt_ratio": 0.8,
                  "overdue_total": 2, "query_3m": 10, "gang_links": 6, "device_risk": 95}},
    {"case_id": "FRAUD-2025-004", "label": "fraud",
     "features": {"multi_platforms": 9, "device_days": 15, "income": 15000, "debt_ratio": 0.75,
                  "overdue_total": 1, "query_3m": 7, "gang_links": 4, "device_risk": 80}},
    {"case_id": "FRAUD-2025-011", "label": "fraud",
     "features": {"multi_platforms": 11, "device_days": 5, "income": 18000, "debt_ratio": 0.85,
                  "overdue_total": 0, "query_3m": 8, "gang_links": 5, "device_risk": 90}},
    {"case_id": "NORMAL-2025-101", "label": "normal",
     "features": {"multi_platforms": 2, "device_days": 400, "income": 28000, "debt_ratio": 0.35,
                  "overdue_total": 0, "query_3m": 2, "gang_links": 0, "device_risk": 5}},
    {"case_id": "NORMAL-2025-102", "label": "normal",
     "features": {"multi_platforms": 1, "device_days": 700, "income": 32000, "debt_ratio": 0.2,
                  "overdue_total": 0, "query_3m": 1, "gang_links": 0, "device_risk": 2}},
    {"case_id": "REVIEW-2025-050", "label": "normal",
     "features": {"multi_platforms": 5, "device_days": 60, "income": 18000, "debt_ratio": 0.55,
                  "overdue_total": 1, "query_3m": 5, "gang_links": 1, "device_risk": 20}},
]


def _featurize(app: LoanApplication, multi_loan: dict, report: dict,
               gang_size: int, device_score: int) -> List[float]:
    """将申请单转为与历史案件同构的特征向量。"""
    raw = {
        "multi_platforms": float(multi_loan.get("platform_count", 0)),
        "device_days": float(app.device.first_seen_days),
        "income": float(app.applicant.monthly_income),
        "debt_ratio": min(1.0, float(multi_loan.get("total_balance", 0)) / 12.0
                          / max(app.applicant.monthly_income, 1.0)),
        "overdue_total": float(report.get("overdue_m1", 0) + report.get("overdue_m2", 0)
                               + report.get("overdue_m3", 0)),
        "query_3m": float(report.get("query_count_3m", 0)),
        "gang_links": float(gang_size),
        "device_risk": float(device_score) / 35.0 * 100.0,
    }
    return [min(1.0, raw[k] / _MILVUS_SCALES[k]) for k in _MILVUS_FEATURE_KEYS]


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class MilvusStore:
    """历史欺诈案件向量相似检索。"""

    def __init__(self):
        self._mock = True
        if settings.MILVUS_ENABLED:
            try:
                from pymilvus import Collection, connections  # 延迟导入

                connections.connect(uri=settings.MILVUS_URI)
                self._collection = Collection("fraud_cases")
                self._mock = False
            except Exception as e:
                logger.warning("Milvus连接失败(%s)，降级为Mock检索", e)

    def search_similar(self, app: LoanApplication, multi_loan: dict, report: dict,
                       gang_size: int, device_score: int, top_k: int = 3) -> List[dict]:
        """检索与当前申请最相似的历史案件: [{case_id, similarity, label}]"""
        vector = _featurize(app, multi_loan, report, gang_size, device_score)
        if self._mock:
            results = []
            for case in _MILVUS_HISTORICAL:
                hist_vec = [min(1.0, case["features"][k] / _MILVUS_SCALES[k])
                            for k in _MILVUS_FEATURE_KEYS]
                results.append({
                    "case_id": case["case_id"],
                    "similarity": round(_cosine(vector, hist_vec), 4),
                    "label": case["label"],
                })
            results.sort(key=lambda r: -r["similarity"])
            return results[:top_k]
        res = self._collection.search([vector], "vector", {"metric_type": "COSINE"},
                                      limit=top_k, output_fields=["case_id", "label"])
        return [{"case_id": hit.entity.get("case_id"), "similarity": hit.distance,
                 "label": hit.entity.get("label")} for hit in res[0]]


# ============================================================
# MinIO适配器（影像/审计文件存储）
# ============================================================
class MinioStore:
    """文件对象存储。"""

    def __init__(self):
        self._mock = True
        self._client = None
        if settings.MINIO_ENABLED:
            try:
                from minio import Minio  # 延迟导入

                self._client = Minio(
                    settings.MINIO_ENDPOINT,
                    access_key=settings.MINIO_ACCESS_KEY,
                    secret_key=settings.MINIO_SECRET_KEY,
                    secure=settings.MINIO_SECURE,
                )
                if not self._client.bucket_exists(settings.MINIO_BUCKET):
                    self._client.make_bucket(settings.MINIO_BUCKET)
                self._mock = False
            except Exception as e:
                logger.warning("MinIO连接失败(%s)，降级为本地文件存储", e)
        self._local_root = settings.DATA_DIR / "minio"

    def save_bytes(self, object_key: str, data: bytes) -> str:
        if self._mock:
            path = self._local_root / object_key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return str(path)
        from io import BytesIO

        self._client.put_object(settings.MINIO_BUCKET, object_key, BytesIO(data), len(data))
        return f"minio://{settings.MINIO_BUCKET}/{object_key}"

    def get_bytes(self, object_key: str) -> Optional[bytes]:
        if self._mock:
            path = self._local_root / object_key
            return path.read_bytes() if path.exists() else None
        try:
            resp = self._client.get_object(settings.MINIO_BUCKET, object_key)
            return resp.read()
        except Exception:
            return None


# ============================================================
# Doris适配器（资产质量分析）
# ============================================================
_MOCK_ASSET_QUALITY = {
    "A": {"delinquency30": 0.004, "delinquency90": 0.001, "loss_rate": 0.002},
    "B": {"delinquency30": 0.010, "delinquency90": 0.003, "loss_rate": 0.005},
    "C": {"delinquency30": 0.030, "delinquency90": 0.010, "loss_rate": 0.015},
    "D": {"delinquency30": 0.070, "delinquency90": 0.030, "loss_rate": 0.040},
    "E": {"delinquency30": 0.150, "delinquency90": 0.080, "loss_rate": 0.100},
}


class DorisStore:
    """列式分析库：资产质量与风险迁徙。"""

    def __init__(self):
        self._mock = True
        self._conn_lock = threading.Lock()
        if settings.DORIS_ENABLED:
            try:
                import pymysql  # 延迟导入（Doris兼容MySQL协议）

                self._conn = pymysql.connect(
                    host=settings.DORIS_HOST, port=settings.DORIS_PORT, user="root",
                )
                self._mock = False
            except Exception as e:
                logger.warning("Doris连接失败(%s)，降级为Mock分析数据", e)

    def get_asset_quality(self, grade: str) -> dict:
        if self._mock:
            return dict(_MOCK_ASSET_QUALITY.get(grade, _MOCK_ASSET_QUALITY["C"]))
        # 真实模式共享单连接，加锁串行化线程池并发调用
        with self._conn_lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT delinquency30, delinquency90, loss_rate FROM t_asset_quality "
                    "WHERE grade=%s", (grade,),
                )
                row = cur.fetchone()
                return dict(zip(("delinquency30", "delinquency90", "loss_rate"), row)) if row else {}


# ============================================================
# 单例注册表
# ============================================================
_mysql_store: Optional[MySQLStore] = None
_milvus_store: Optional[MilvusStore] = None
_minio_store: Optional[MinioStore] = None
_doris_store: Optional[DorisStore] = None


def get_mysql_store() -> MySQLStore:
    global _mysql_store
    if _mysql_store is None:
        _mysql_store = MySQLStore()
    return _mysql_store


def get_milvus_store() -> MilvusStore:
    global _milvus_store
    if _milvus_store is None:
        _milvus_store = MilvusStore()
    return _milvus_store


def get_minio_store() -> MinioStore:
    global _minio_store
    if _minio_store is None:
        _minio_store = MinioStore()
    return _minio_store


def get_doris_store() -> DorisStore:
    global _doris_store
    if _doris_store is None:
        _doris_store = DorisStore()
    return _doris_store
