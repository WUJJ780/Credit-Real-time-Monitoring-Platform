"""统一状态定义：Agent结构化输出模型 + 流程状态字典（单一事实源）。

本模块集中定义系统全部的「结构化输出Pydantic模型」与「状态字典」，
按职责分为五个层次：

  1. 通用枚举         —— 环节结论/后续动作/风险等级等全局取值域
  2. Agent输出模型     —— 7个Agent（进件/反欺诈/信用/审批/合规/贷后/中台）
                         的Pydantic结构化输出，Agent之间唯一的数据契约
  3. 流程上下文/状态   —— PipelineContext（贯穿编排的上下文）、
                         WorkflowState（LangGraph图状态字典）、
                         异步流水线状态（受理状态/统计/查询与事件载荷）
  4. 消息事件信封      —— EventEnvelope（Kafka/内存总线消息封装）
  5. 子结构模型        —— 各Agent输出内嵌的评估明细（黑名单命中、
                         团伙检测、评分卡、还款能力等）

为什么集中在此（agents/resume/state.py）：
  - LangGraph以申请单号作thread_id做检查点，流程中断后可恢复(resume)，
    恢复依赖状态模型的稳定与可追溯——集中定义即「单一事实源」；
  - 新增Agent：继承AgentResult定义输出模型并在此登记，即可被
    规则引擎、审计、消息总线、流水线一致消费；
  - 兼容导入：models/risk.py 保留原路径（re-export本模块），
    旧代码 from credit_risk_system.models.risk import X 继续可用。

边界约定：
  - 输入业务模型（LoanApplication/Applicant/DeviceInfo/LoanRequest/
    Document等）见 models/loan.py —— 输入而非结构化输出；
  - LLM Function Calling工具参数schema（query_blacklist等参数模型）
    见 core/tools.py —— 工具入参而非Agent输出。
"""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, TypedDict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from credit_risk_system.models.loan import LoanApplication


# ============================================================
# 一、通用枚举（全局取值域）
# ============================================================
class AgentStatus(str, Enum):
    """各风控环节的通用结论枚举。

    生产者：每个Agent的规则引擎/LLM/兜底层。
    消费者：编排器条件路由（orchestrator._route_after_*）、
            决策总控（FinalDecision.stage_results）、审计留痕。
    """
    PASS = "PASS"                    # 本环节通过，流程继续
    REJECT = "REJECT"                # 本环节否决
    SUPPLEMENT = "SUPPLEMENT"        # 要求补件
    MANUAL_REVIEW = "MANUAL_REVIEW"  # 转人工复核
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"              # 环节跳过


class NextAction(str, Enum):
    """环节结论对应的后续动作（流程控制语义）。

    生产者：各Agent输出模型（AgentResult.next_action）。
    消费者：决策总控与流程编排（声明式语义，实际路由由
            orchestrator的conditional_edges实现）。
    """
    CONTINUE = "CONTINUE"       # 进入下一环节
    STOP = "STOP"               # 终止流程（拒绝）
    SUPPLEMENT = "SUPPLEMENT"   # 终止流程（补件）
    ESCALATE = "ESCALATE"       # 升级人工/异常升级
    FINALIZE = "FINALIZE"       # 进入汇总


class FraudLevel(str, Enum):
    """反欺诈风险等级（由欺诈分按FRAUD_BANDS阈值映射）。"""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    SEVERE = "SEVERE"


class CreditGrade(str, Enum):
    """信用等级（由信用评分按GRADE_BANDS阈值映射，A最优E最差）。"""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"


class RiskLevel(str, Enum):
    """贷后风险分级（贷后管理Agent输出，决定监控频率与催收策略）。"""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"


class ApprovalDecision(str, Enum):
    """审批决策枚举（审批决策Agent输出）。"""
    AUTO_APPROVE = "AUTO_APPROVE"      # 自动审批通过
    AUTO_REJECT = "AUTO_REJECT"        # 自动拒绝
    MANUAL_REVIEW = "MANUAL_REVIEW"    # 转人工复核


class FinalStatus(str, Enum):
    """最终决策枚举（风控中台Agent汇总输出，对外业务结论）。"""
    APPROVED = "APPROVED"              # 审批通过
    REJECTED = "REJECTED"              # 审批拒绝
    MANUAL_REVIEW = "MANUAL_REVIEW"    # 转人工复核
    SUPPLEMENT = "SUPPLEMENT"          # 补件


class ApplicationStatus(str, Enum):
    """异步流水线中单笔申请的受理/处理状态（AsyncRiskPipeline维护）。

    生命周期：SUBMITTED(排队) -> PROCESSING -> COMPLETED / FAILED / TIMEOUT；
    DUPLICATE为幂等受理标记（重复提交同一申请单号时返回原单）。
    """
    SUBMITTED = "SUBMITTED"      # 已受理，排队中
    PROCESSING = "PROCESSING"    # 处理中
    COMPLETED = "COMPLETED"      # 已完成
    FAILED = "FAILED"            # 处理异常
    TIMEOUT = "TIMEOUT"          # 处理超时
    DUPLICATE = "DUPLICATE"      # 重复提交（幂等受理）


# ============================================================
# 二、通用基础模型
# ============================================================
class AuditEvent(BaseModel):
    """审计留痕事件（全流程可追溯的合规底座）。

    作用：记录每一次决策与异常，供合规审计、监管报送与事后追溯；
    生产者：PipelineContext.record_event()（所有Agent与节点异常处理）；
    消费者：合规审计Agent（审计留痕完整性规则）、MinIO审计件、
            output/<申请单号>_decision.json 留痕文件。
    """
    ts: datetime = Field(default_factory=datetime.now)
    agent: str                                    # 事件来源Agent名
    layer: str                                    # 决策层：LLM推理/规则引擎/默认兜底/system
    level: str = "INFO"                           # INFO / WARNING / VIOLATION
    message: str


class AgentResult(BaseModel):
    """所有Agent结构化输出的基类（Pydantic契约）。

    作用：统一各Agent输出的公共元信息（结论/摘要/决策层/耗时），
    保证输出可校验、可审计、可被下游Agent确定性消费；
    生产者：每个Agent（BaseAgent.run/arun在输出前统一回填元信息）；
    消费者：编排器路由、后续Agent的证据采集、审计与消息总线。
    """
    model_config = ConfigDict(extra="ignore")

    agent: str = Field(default="", description="Agent名称")
    status: AgentStatus = Field(description="环节结论")
    summary: str = Field(description="结论摘要")
    next_action: NextAction = Field(default=NextAction.CONTINUE, description="后续动作")
    detail: str = Field(default="", description="详细说明")
    layer: str = Field(default="", description="决策层：LLM推理/规则引擎/默认兜底")
    executed_at: datetime = Field(default_factory=datetime.now)
    elapsed_ms: int = Field(default=0)


# ============================================================
# 三、进件审核Agent输出
# ============================================================
class IntakeResult(AgentResult):
    """进件审核Agent（初审岗）的结构化输出。

    作用：资料完整性、三要素核验、OCR质量与基础准入规则的审核结论；
    生产者：IntakeAgent（LLM/规则引擎/兜底三层）；
    消费者：编排器路由（PASS→反欺诈，REJECT/SUPPLEMENT→合规审计→总控）、
            审批Agent（completeness_score折算进件风险分）。
    """
    completeness_score: float = Field(description="资料完整度评分(0-100)")
    identity_verified: bool = Field(description="三要素核验是否通过")
    ocr_confidence: float = Field(default=0, description="OCR识别置信度")
    missing_documents: List[str] = Field(default_factory=list, description="缺失资料清单")
    warnings: List[str] = Field(default_factory=list, description="提示信息")
    rules_triggered: List[str] = Field(default_factory=list, description="命中的规则")


# ============================================================
# 四、反欺诈Agent输出（含子结构）
# ============================================================
class BlacklistHit(BaseModel):
    """黑名单命中明细。

    作用：承载司法/失信/行内/同业黑名单的匹配详情；
    生产者：MySQLStore.get_blacklist / 工具query_blacklist；
    消费者：AntiFraudResult.blacklist_hits（审计与拒绝理由）。
    """
    list_type: str = Field(description="名单类型")
    source: str = Field(description="数据来源")
    matched_on: str = Field(description="命中字段")
    level: str = Field(description="严重程度")
    detail: str = Field(default="")


class MultiLoanAssessment(BaseModel):
    """多头借贷评估明细。

    作用：借款平台数、近7日查询次数与在贷余额的评估结果；
    生产者：MySQLStore.get_multi_loan（反欺诈Agent证据采集）；
    消费者：反欺诈规则引擎（多头/查询频繁规则）与反欺诈评分。
    """
    platform_count: int = Field(default=0, description="借贷平台数")
    query_7d: int = Field(default=0, description="近7日借贷查询次数")
    total_balance: float = Field(default=0, description="在贷总余额（元）")
    platforms: List[str] = Field(default_factory=list, description="平台清单")


class DeviceRiskAssessment(BaseModel):
    """设备风险评估明细。

    作用：设备ROOT/模拟器/代理/多账户等风险信号的评估结果；
    生产者：compute_device_risk_score（反欺诈Agent证据采集）；
    消费者：反欺诈规则引擎（设备信号规则）与反欺诈评分。
    """
    rooted: bool = False
    emulator: bool = False
    proxy: bool = False
    ip_mismatch: bool = False
    first_seen_days: int = 365
    account_count: int = 1
    score: int = Field(default=0, description="设备风险分")
    signals: List[str] = Field(default_factory=list, description="命中的设备风险信号")


class GangDetection(BaseModel):
    """团伙欺诈识别（知识图谱）结果。

    作用：知识图谱广度遍历（设备/手机/IP共享关系）的团伙检测结论；
    生产者：FraudGraph.detect_gangs（Neo4j/内存图）；
    消费者：反欺诈Agent（团伙关联规则与评分）、工具detect_gang。
    """
    gang_id: Optional[str] = Field(default=None, description="团伙编号")
    component_size: int = Field(default=1, description="关联组件规模")
    relation: str = Field(default="", description="关联关系")
    linked_fraud_case: Optional[str] = Field(default=None, description="关联的历史欺诈案件")
    score: float = Field(default=0, description="团伙风险分")


class SimilarCase(BaseModel):
    """历史相似案件（Milvus向量检索结果单条）。

    作用：当前申请与历史欺诈案件的向量相似度比对结果；
    生产者：MilvusStore.search_similar；
    消费者：反欺诈Agent（相似欺诈案件规则）。
    """
    case_id: str
    similarity: float
    label: str = Field(description="案件标签：fraud/normal")


class AntiFraudResult(AgentResult):
    """反欺诈Agent（反欺诈岗）的结构化输出。

    作用：黑名单/多头/设备/团伙/相似案件五大维度的综合欺诈评估，
    SEVERE等级触发自动拒绝（一票否决）；
    生产者：AntiFraudAgent；
    消费者：编排器路由、审批Agent（fraud_level一票否决/转人工规则）、
            贷后Agent（HIGH/SEVERE提升贷后风险等级）。
    """
    fraud_score: float = Field(description="反欺诈评分(0-100)")
    fraud_level: FraudLevel = Field(description="欺诈风险等级")
    fraud_tags: List[str] = Field(default_factory=list, description="欺诈风险标签")
    blacklist_hits: List[BlacklistHit] = Field(default_factory=list)
    multi_loan: MultiLoanAssessment = Field(default_factory=MultiLoanAssessment)
    device_risk: DeviceRiskAssessment = Field(default_factory=DeviceRiskAssessment)
    gang: GangDetection = Field(default_factory=GangDetection)
    similar_cases: List[SimilarCase] = Field(default_factory=list)


# ============================================================
# 五、信用评估Agent输出（含子结构）
# ============================================================
class CreditReportSummary(BaseModel):
    """征信报告摘要。

    作用：人行征信报告的标准化摘要（逾期/授信/查询等维度）；
    生产者：MySQLStore.get_credit_report；
    消费者：信用评估Agent（硬拒绝规则与评分卡特征）。
    """
    has_report: bool = True
    first_loan_years: float = Field(default=0, description="首贷距今（年）")
    overdue_m1: int = Field(default=0, description="近24个月M1逾期次数")
    overdue_m2: int = Field(default=0, description="近24个月M2逾期次数")
    overdue_m3: int = Field(default=0, description="近24个月M3+逾期次数")
    overdue_amount: float = Field(default=0, description="当前逾期金额")
    total_limit: float = Field(default=0, description="授信总额")
    used_limit: float = Field(default=0, description="已用额度")
    utilization: float = Field(default=0, description="额度使用率")
    query_count_3m: int = Field(default=0, description="近3个月征信查询次数")
    credit_cards: int = Field(default=0, description="信用卡张数")
    special_records: List[str] = Field(default_factory=list, description="特殊交易/公共记录")

    @property
    def overdue_total(self) -> int:
        """M1/M2/M3逾期总次数（硬拒绝规则入参）。"""
        return self.overdue_m1 + self.overdue_m2 + self.overdue_m3


class RepaymentAbility(BaseModel):
    """还款能力评估。

    作用：等额本息月供、收入负债比(DTI)、自由现金流的测算结果；
    生产者：信用评估Agent（月供系数×金额、已有负债估算）；
    消费者：信用硬拒绝规则（DTI>75%）、审批Agent（负债比复核规则）。
    """
    monthly_income: float = 0
    existing_monthly_debt: float = Field(default=0, description="已有月负债")
    new_loan_monthly_payment: float = Field(default=0, description="本笔贷款月供")
    dti: float = Field(default=0, description="收入负债比")
    dti_cap: float = Field(default=0, description="收入负债比上限")
    free_cash_flow: float = Field(default=0, description="月自由现金流")
    assessment: str = Field(default="", description="评估结论")


class ScoreCardResult(BaseModel):
    """信用评分卡模型结果。

    作用：评分卡总分、信用等级与特征得分明细；
    生产者：ScoreCard.evaluate（本地评分卡，PAI云端模型兜底切换）；
    消费者：信用评估Agent（等级/额度倍数）、审批Agent（等级复核规则）。
    """
    total_score: int = Field(description="信用评分(0-100)")
    grade: CreditGrade = Field(description="信用等级")
    max_score: int = 100
    breakdown: Dict[str, float] = Field(default_factory=dict, description="各特征得分")
    model_version: str = ""


class CreditResult(AgentResult):
    """信用评估Agent（风控岗）的结构化输出。

    作用：征信/评分卡/还款能力的综合评估与额度初判；
    生产者：CreditAgent；
    消费者：审批Agent（信用分/等级/建议额度入综合风险与额度核定）、
            贷后Agent（信用等级作为贷后风险分级基准）。
    """
    credit_score: int = Field(description="信用评分(0-100)")
    credit_grade: CreditGrade = Field(description="信用等级")
    scorecard: ScoreCardResult = Field(description="评分卡明细")
    repayment_ability: RepaymentAbility = Field(description="还款能力评估")
    credit_report: CreditReportSummary = Field(description="征信报告摘要")
    suggested_max_amount: float = Field(default=0, description="建议最高额度")
    income_multiple: float = Field(default=0, description="适用收入倍数")


# ============================================================
# 六、审批决策Agent输出
# ============================================================
class ApprovalResult(AgentResult):
    """审批决策Agent（审批岗）的结构化输出。

    作用：综合反欺诈/信用/进件三个环节风险分后给出审批决策、
    最终额度、利率定价与放款条件；
    生产者：ApprovalAgent；
    消费者：编排器路由（AUTO_APPROVE/MANUAL_REVIEW→贷后管理）、
            合规Agent（利率/用途合规校验）、决策总控（汇总最终决策）。
    """
    decision: ApprovalDecision = Field(description="审批决策")
    approved_amount: Optional[float] = Field(default=None, description="最终额度")
    approved_rate: Optional[float] = Field(default=None, description="最终年化利率(%)")
    term_months: int = Field(default=0, description="借款期限")
    composite_risk_score: float = Field(default=0, description="综合风险分")
    pricing_basis: str = Field(default="", description="定价依据")
    loan_conditions: List[str] = Field(default_factory=list, description="放款条件")
    rule_conflicts: List[str] = Field(default_factory=list, description="规则冲突记录")
    reasons: List[str] = Field(default_factory=list, description="决策理由")


# ============================================================
# 七、合规审计Agent输出（含子结构）
# ============================================================
class ComplianceIssue(BaseModel):
    """合规问题单项。

    作用：单条合规规则的检查结论（违规项+整改提示）；
    生产者：合规审计Agent（利率4倍LPR/24%、资金用途、PIPL授权、
            征信授权、审计留痕完整性规则）；
    消费者：决策总控（VIOLATION触发一票否决）、合规报告。
    """
    rule: str = Field(description="合规规则")
    level: str = Field(description="级别：WARNING / VIOLATION")
    description: str = ""
    remediation: str = Field(default="", description="整改提示")


class ComplianceResult(AgentResult):
    """合规审计Agent（合规岗）的结构化输出。

    作用：全流程合规检查结论（passed=false且有VIOLATION时触发
    决策总控一票否决）；
    生产者：ComplianceAgent；
    消费者：编排器路由（不通过→直接决策总控）、
            决策总控（一票否决/合规通过标记）。
    """
    passed: bool = Field(description="合规检查是否通过")
    issues: List[ComplianceIssue] = Field(default_factory=list)
    checked_rules: List[str] = Field(default_factory=list, description="已检查的规则清单")
    report_ref: str = Field(default="", description="合规报告引用")


# ============================================================
# 八、贷后管理Agent输出
# ============================================================
class PostLoanResult(AgentResult):
    """贷后管理Agent（贷后岗）的结构化输出。

    作用：贷后风险分级、监控频率、催收策略与风险迁徙预估；
    生产者：PostLoanAgent（信用等级为基准，反欺诈HIGH/SEVERE升一级，
            结合Doris资产质量数据）；
    消费者：决策总控（stage_results留痕）、贷后系统（放款后监控执行）。
    """
    risk_level: RiskLevel = Field(description="贷后风险分级")
    monitoring_frequency: str = Field(default="", description="监控频率")
    collection_strategy: str = Field(default="", description="催收策略")
    alert_thresholds: List[str] = Field(default_factory=list, description="预警阈值")
    expected_migration: str = Field(default="", description="预期风险迁徙")
    pd_estimate: float = Field(default=0, description="预估违约概率")


# ============================================================
# 九、风控中台Agent最终决策输出
# ============================================================
class FinalDecision(BaseModel):
    """风控中台Agent汇总输出的最终决策（对外的业务结论）。

    作用：汇总各环节结论、冲突裁决（合规一票否决、审批结论映射）、
    组装额度/利率/理由链，并落库/发布/审计；
    生产者：MiddlePlatformAgent.finalize；
    消费者：AsyncRiskPipeline（COMPLETED终态）、MySQL/MinIO/消息总线
            持久化、跨申请记忆AgentMemory、业务接入层。
    """
    model_config = ConfigDict(extra="ignore")

    application_id: str = Field(description="申请单号")
    decision: FinalStatus = Field(description="最终决策")
    final_amount: Optional[float] = Field(default=None, description="最终额度")
    final_rate: Optional[float] = Field(default=None, description="最终年化利率(%)")
    term_months: Optional[int] = Field(default=None, description="借款期限")
    reasons: List[str] = Field(default_factory=list, description="决策理由链")
    fraud_score: Optional[float] = Field(default=None,
                                         description="反欺诈评分(0-100)，未进入反欺诈环节时为None")
    fraud_tags: List[str] = Field(default_factory=list,
                                  description="反欺诈命中信号标签（设备/黑名单/多头/团伙等）")
    compliance_passed: bool = Field(default=True)
    supplement_notice: Optional[str] = Field(default=None, description="补件提示")
    stage_results: Dict[str, str] = Field(default_factory=dict, description="各环节结论")
    audit_ref: str = Field(default="", description="审计引用编号")
    decided_at: datetime = Field(default_factory=datetime.now)
    decided_by: str = Field(default="middle_platform_agent", description="决策主体")


# ============================================================
# 十、流程上下文（贯穿整个LangGraph编排）
# ============================================================
class PipelineContext(BaseModel):
    """风控流水线上下文：承载申请单、各Agent输出、证据与审计事件。

    作用：单笔申请在整条流程中的「内存状态载体」，随LangGraph状态
    在节点间传递（WorkflowState.context），每笔申请独立实例；
    生产者：orchestrator._initial_state；
    消费者：全部Agent（读写本岗位结果与证据）、编排器路由与总控。
    """
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    application: LoanApplication = Field(description="进件申请")
    evidence: Dict[str, Any] = Field(default_factory=dict, description="各环节采集的证据")
    intake_result: Optional[IntakeResult] = None
    antifraud_result: Optional[AntiFraudResult] = None
    credit_result: Optional[CreditResult] = None
    approval_result: Optional[ApprovalResult] = None
    compliance_result: Optional[ComplianceResult] = None
    postloan_result: Optional[PostLoanResult] = None
    final_decision: Optional[FinalDecision] = None
    audit_events: List[AuditEvent] = Field(default_factory=list, description="审计留痕")

    def record_event(self, agent: str, layer: str, message: str, level: str = "INFO") -> None:
        """追加审计事件（供合规审计与监管报送）。"""
        from credit_risk_system.core.logger import get_logger

        self.audit_events.append(
            AuditEvent(agent=agent, layer=layer, level=level, message=message)
        )
        get_logger("audit").info("[%s][%s] %s", agent, layer, message)


# ============================================================
# 十一、LangGraph流程状态字典
# ============================================================
class WorkflowState(TypedDict, total=False):
    """LangGraph流程状态字典（图节点间传递与检查点持久化的状态）。

    作用：StateGraph的State类型——每个节点读入并回写该字典，
    检查点(checkpointer, thread_id=申请单号)按节点持久化，
    支持流程中断后从最近检查点恢复(resume)；
    生产者/消费者：orchestrator同步图与异步图的全部节点。
    """
    application_id: str                  # 申请单号（=检查点thread_id）
    context: PipelineContext             # 流程上下文（各Agent输出与审计事件）
    stage: str                           # 当前环节名（dispatch/intake/.../finalize）
    errors: List[str]                    # 节点执行异常记录（不中断流程，留痕继续）


# ============================================================
# 十二、异步流水线状态（AsyncRiskPipeline）
# ============================================================
class PipelineStats:
    """异步流水线运行统计（非Pydantic——运行时指标而非持久化状态）。

    作用：吞吐、耗时分布（min/avg/p50/p90/p99/max）、终态计数，
    供 GET /api/v1/stats 与压测脚本观测并发处理能力；
    维护者：AsyncRiskPipeline._set/_refresh_stats_counts。
    """
    def __init__(self):
        self.submitted: int = 0        # 累计受理数
        self.completed: int = 0        # 完成数
        self.failed: int = 0           # 异常数
        self.timed_out: int = 0        # 超时数
        self.in_flight: int = 0        # 当前处理中
        self.queued: int = 0           # 当前排队中
        self.total_elapsed_ms: int = 0
        self.min_elapsed_ms: int = 0
        self.max_elapsed_ms: int = 0
        self.latencies_ms: List[int] = []  # 已完成申请的耗时样本（分位统计用）

    def percentile(self, pct: float) -> int:
        """耗时样本的pct分位（0~1），无样本返回0。"""
        if not self.latencies_ms:
            return 0
        data = sorted(self.latencies_ms)
        idx = min(len(data) - 1, int(len(data) * pct))
        return data[idx]

    def to_dict(self) -> dict:
        """转为API统计载荷（avg/p50/p90/p99为计算字段）。"""
        n = self.completed + self.failed + self.timed_out
        return {
            "submitted": self.submitted,
            "in_flight": self.in_flight,
            "queued": self.queued,
            "completed": self.completed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "avg_elapsed_ms": int(self.total_elapsed_ms / n) if n else 0,
            "p50_ms": self.percentile(0.50),
            "p90_ms": self.percentile(0.90),
            "p99_ms": self.percentile(0.99),
            "min_elapsed_ms": self.min_elapsed_ms,
            "max_elapsed_ms": self.max_elapsed_ms,
        }


class StatusPayload(TypedDict, total=False):
    """GET /api/v1/applications/{id} 查询载荷（异步流水线）。"""
    application_id: str                 # 申请单号
    status: str                         # ApplicationStatus取值
    stage: str                          # 当前/最后完成的环节
    elapsed_ms: int                     # 已耗时（终态为总耗时）
    error: str                          # 失败/超时原因
    final_decision: Dict[str, Any]      # 终态时附最终决策（FinalDecision序列化）
    audit_events: List[Dict[str, Any]]  # 终态时附全流程审计事件链（AuditEvent序列化）


class PipelineEvent(TypedDict, total=False):
    """SSE事件载荷（AsyncRiskPipeline._emit推送，events队列消费）。"""
    event: str                          # submitted/processing/stage/completed/failed/timeout
    application_id: str                 # 申请单号
    stage: str                          # event=stage时的环节名
    decision: str                       # event=completed时的最终决策值
    error: str                          # event=failed/timeout时的原因
    final_decision: Dict[str, Any]      # event=completed时附最终决策
    ts: float                           # 事件时间戳


# ============================================================
# 十三、消息总线事件信封
# ============================================================
class EventEnvelope(BaseModel):
    """Kafka/内存消息总线的事件信封。

    作用：Agent间通讯与外部订阅的统一消息封装（主题约定：
    risk.<agent_name>环节结果、risk.decision最终决策、risk.alerts告警）；
    生产者：各Agent与中台（结果事件/决策事件/升级事件）；
    消费者：MessageBus订阅方（Kafka消费线程/内存订阅者）。
    """
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    topic: str                                        # 事件主题
    source: str = Field(default="", description="来源Agent")
    application_id: str = Field(default="", description="申请单号")
    event_type: str = Field(default="", description="事件类型")
    payload: dict = Field(default_factory=dict)       # 事件载荷（结构化输出序列化）
    ts: datetime = Field(default_factory=datetime.now)
