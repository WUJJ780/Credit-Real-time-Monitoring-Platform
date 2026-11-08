"""风控策略配置中心。

所有阈值、权重、评分卡、定价、合规规则集中在此配置，
支持策略迭代（修改配置即可上线新策略，无需改动Agent代码）。
"""
from credit_risk_system.models.loan import DocType

# ============================================================
# 一、进件审核策略
# ============================================================
AGE_MIN, AGE_MAX = 22, 60                  # 准入年龄
INCOME_MIN = 3000.0                        # 最低月收入（元）
AMOUNT_MIN, AMOUNT_MAX = 1000.0, 500000.0  # 申请金额区间（元）
TERM_MIN, TERM_MAX = 3, 60                 # 借款期限区间（月）
PHONE_REG_WARN_MONTHS = 6                  # 手机号注册时长提醒阈值（月）

# 必需资料及完整度权重（合计100）
REQUIRED_DOCS = (DocType.ID_FRONT, DocType.ID_BACK, DocType.INCOME_PROOF, DocType.FACE_VIDEO)
DOC_WEIGHTS = {
    DocType.ID_FRONT: 25,
    DocType.ID_BACK: 25,
    DocType.INCOME_PROOF: 30,
    DocType.FACE_VIDEO: 20,
}
BUSINESS_LICENSE_DOC = DocType.BUSINESS_LICENSE   # 经营贷需补充营业执照
OCR_CONFIDENCE_MIN = 0.6                  # OCR置信度下限（低于则影像不合格）
OCR_CONFIDENCE_WARN = 0.8                 # OCR置信度提醒阈值

# ============================================================
# 二、反欺诈策略
# ============================================================
# 欺诈分等级区间: (分数下限, 等级)
FRAUD_BANDS = ((80, "SEVERE"), (60, "HIGH"), (30, "MEDIUM"), (0, "LOW"))
FRAUD_SCORE_CAP = 100

# 各模块得分上限（防止单模块主导总分）
MODULE_CAPS = {"blacklist": 55, "multiloan": 25, "device": 35, "gang": 30, "similar": 15}

# 黑名单类型命中权重
BLACKLIST_WEIGHTS = {
    "司法": 55,        # 司法被执行人
    "失信": 55,        # 失信被执行人
    "行内黑名单": 40,
    "同业黑名单": 30,
}

# 多头借贷规则: 平台数达到N加X分（取最高命中档）
MULTILOAN_RULES = ((8, 25), (6, 18), (4, 12), (3, 8))
# 近7日借贷查询次数规则
QUERY_7D_RULES = ((6, 15), (4, 8), (3, 5))

# 设备风险信号: (信号, 加分)
DEVICE_SIGNALS = (
    ("rooted", 20),        # 越狱/ROOT
    ("emulator", 15),      # 模拟器
    ("proxy", 10),         # 代理/VPN
    ("new_device_7d", 12), # 设备首次出现不足7天
    ("new_device_30d", 5), # 设备首次出现不足30天
    ("multi_account", 8),  # 单设备多账户
    ("ip_mismatch", 10),   # IP归属地与常驻地不一致
)
DEVICE_MULTI_ACCOUNT_MIN = 4

# 团伙欺诈（知识图谱）
GANG_FRAUD_LINK_SCORE = 30     # 与已确认欺诈团伙/案件关联
GANG_SHARED_DEVICE_SCORE = 6   # 与非欺诈申请人共享设备

# 相似欺诈案件（Milvus向量检索）
SIMILAR_CASE_THRESHOLD = 0.8   # 相似度阈值
SIMILAR_FRAUD_SCORE = 15

# ============================================================
# 三、信用评估策略
# ============================================================
DTI_CAP = 0.55                 # 收入负债比上限（月供+负债 / 月收入）
DTI_HARD_REJECT = 0.75         # 收入负债比硬拒绝线

# 信用等级: (分数下限, 等级)
GRADE_BANDS = ((85, "A"), (70, "B"), (55, "C"), (40, "D"), (0, "E"))
# 各等级收入倍数上限（核定额度参考）
INCOME_MULTIPLES = {"A": 8, "B": 6, "C": 4, "D": 2, "E": 0}

# 评分卡配置（weight: 特征权重; mapping: 类别型取值映射; bins: 数值型分箱）
SCORECARD_VERSION = "scorecard-v1.0"
SCORECARD_CONFIG = (
    {"name": "age", "weight": 1.0, "bins": ((26, 35, 5), (36, 50, 4), (22, 25, 3), (51, 60, 2))},
    {"name": "education", "weight": 1.0,
     "mapping": {"硕士及以上": 5, "本科": 4, "大专": 3, "高中": 2, "中专及以下": 1}},
    {"name": "income", "weight": 1.5, "bins": ((30000, None, 5), (20000, 30000, 4), (10000, 20000, 3), (5000, 10000, 2), (0, 5000, 1))},
    {"name": "work_years", "weight": 1.0, "bins": ((5, None, 5), (3, 5, 4), (1, 3, 3), (0, 1, 1))},
    {"name": "marital", "weight": 0.5, "mapping": {"已婚": 5, "未婚": 3, "离异": 2, "丧偶": 2}},
    {"name": "housing", "weight": 0.5, "mapping": {"自有": 5, "按揭": 4, "租房": 2, "其他": 1}},
    {"name": "history_years", "weight": 1.5, "bins": ((5, None, 5), (3, 5, 3), (1, 3, 2), (0, 1, 1))},
    {"name": "overdue_total", "weight": 2.0, "bins": ((0, 0, 5), (1, 1, 3), (2, 2, 2), (3, None, 0))},
    {"name": "utilization", "weight": 1.0, "bins": ((0, 0.3, 5), (0.3, 0.6, 3), (0.6, 0.8, 2), (0.8, None, 0))},
    {"name": "query_3m", "weight": 1.0, "bins": ((0, 2, 5), (3, 5, 3), (6, 8, 1), (8, None, 0))},
    {"name": "income_evidence", "weight": 0.5,
     "mapping": {"银行流水": 5, "税单": 4, "工资单": 4, "自报": 1}},
)

# ============================================================
# 四、审批决策策略
# ============================================================
# 综合风险分权重: 反欺诈 / 信用 / 进件
COMPOSITE_WEIGHTS = {"fraud": 0.30, "credit": 0.45, "intake": 0.25}
AUTO_APPROVE_RISK = 35.0   # 综合风险分 < 35 自动通过
AUTO_REJECT_RISK = 65.0    # 综合风险分 >= 65 自动拒绝

# 定价：1年期LPR（%），按监管发布更新
LPR_1Y = 3.45
GRADE_SPREADS = {"A": 2.5, "B": 3.5, "C": 5.0, "D": 7.0, "E": 0.0}
# 利率合规上限：4倍LPR（民间借贷司法保护上限）与 24%（两线三区）
RATE_CAP_LPR_MULTIPLE = 4.0
RATE_CAP_ABSOLUTE = 24.0

# 人工复核触发条件说明（用于审批结果理由展示）
MANUAL_REVIEW_REASONS = (
    "反欺诈等级为MEDIUM/HIGH",
    "信用等级C及以下",
    "收入负债比超上限",
    "合规检查未通过",
    "综合风险分处于灰色区间",
)

# ============================================================
# 五、贷后管理策略
# ============================================================
# 各等级预估违约概率（PD，年度化）
PD_BY_GRADE = {"A": 0.005, "B": 0.01, "C": 0.03, "D": 0.06, "E": 0.12}
# 12个月风险等级迁徙矩阵（历史资产质量统计）
MIGRATION_MATRIX = {
    "A": {"B": 0.050, "C": 0.010, "D": 0.004, "E": 0.001},
    "B": {"C": 0.060, "D": 0.020, "E": 0.008},
    "C": {"D": 0.080, "E": 0.030},
    "D": {"E": 0.100},
    "E": {"E": 1.000},
}
# 各贷后等级监控方案
MONITORING_PLANS = {
    "A": {"frequency": "月度监控", "strategy": "短信+APP自动提醒", "thresholds": ("逾期≥3天触发提醒",)},
    "B": {"frequency": "月度监控", "strategy": "短信+智能外呼", "thresholds": ("逾期≥3天触发提醒", "逾期≥7天升级人工")},
    "C": {"frequency": "双周监控", "strategy": "人工台账+电话提醒", "thresholds": ("逾期≥1天触发提醒", "逾期≥3天升级催收")},
    "D": {"frequency": "每周监控", "strategy": "人工重点监控+提前介入", "thresholds": ("放款后7天首访", "逾期≥1天升级催收")},
    "E": {"frequency": "每日监控", "strategy": "专项催收团队", "thresholds": ("到期前3天预警", "逾期即催收")},
}

# ============================================================
# 六、合规审计策略
# ============================================================
# 监管禁止的资金用途（命中即合规一票否决）
PROHIBITED_PURPOSE_KEYWORDS = ("股票", "炒股", "期货", "虚拟货币", "数字货币", "购房", "首付", "赌博", "民间借贷")
# 个人信息保护（PIPL）：必须获得授权
REQUIRE_PRIVACY_CONSENT = True
# 征信查询必须获得授权
REQUIRE_CREDIT_AUTHORIZATION = True
