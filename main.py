"""信贷自动风控多Agent系统 —— 主程序入口。

用法：
    python main.py                 # 串行运行全部演示案例（同步模式）
    python main.py --case normal   # 运行指定案例: normal/fraud/review/supplement/compliance
    python main.py --concurrent    # 高并发异步模式：全部案例并发处理（asyncio流水线）
    python server.py               # 启动异步API服务（FastAPI，高并发入口）

说明：
    - 未配置LLM_API_KEY时，Agent由「规则引擎」层决策（离线可运行）；
    - 数据层（MySQL/Kafka/Neo4j/Milvus/MinIO/Doris）未启用时自动使用Mock实现；
    - 完整决策与审计留痕保存于 output/<申请单号>_decision.json。
"""
import argparse
import asyncio
import json
import sys
import time
import warnings
from datetime import date

# 抑制LangGraph内部依赖的弃用提示（不影响功能）
warnings.filterwarnings("ignore", message=".*allowed_objects.*")

from credit_risk_system.config.settings import settings
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.orchestrator import get_orchestrator
from credit_risk_system.models.loan import (
    Channel, DeviceInfo, DocType, Document, Education, EmploymentType,
    HousingStatus, IncomeEvidence, LoanApplication, LoanPurpose, LoanRequest,
    MaritalStatus, Applicant,
)
from credit_risk_system.models.risk import PipelineContext

logger = get_logger("main")

LINE = "=" * 92
THIN = "-" * 92

# 阶段中文名与结果字段映射
STAGES = {
    "dispatch": ("风控中台受理", None),
    "intake": ("进件审核", "intake_result"),
    "antifraud": ("反欺诈", "antifraud_result"),
    "credit": ("信用评估", "credit_result"),
    "approval": ("审批决策", "approval_result"),
    "compliance": ("合规审计", "compliance_result"),
    "postloan": ("贷后管理", "postloan_result"),
    "finalize": ("决策总控", "final_decision"),
}


# ============================================================
# 演示案例构造
# ============================================================
def _docs(application_id: str, *types: DocType) -> list:
    return [Document(doc_type=t, file_key=f"{application_id}/{t.name.lower()}.jpg")
            for t in types]


def build_demo_cases() -> dict:
    """构造5个覆盖不同流程分支的演示案例。"""
    all_docs = (DocType.ID_FRONT, DocType.ID_BACK, DocType.INCOME_PROOF, DocType.FACE_VIDEO)

    # 案例1: 优质客户 -> 自动审批通过
    normal = LoanApplication(
        application_id="APP20260001", channel=Channel.APP,
        applicant=Applicant(
            applicant_id="CUST-001", name="王小明", id_number="110101199503123456",
            phone="13800000001", birthday=date(1995, 3, 12), gender="男",
            education=Education.BACHELOR, marital_status=MaritalStatus.MARRIED,
            housing_status=HousingStatus.MORTGAGED, employment_type=EmploymentType.FULL_TIME,
            employer="星辰互联网科技有限公司", job_title="产品经理", work_years=3,
            monthly_income=30000, income_evidence=IncomeEvidence.BANK_FLOW,
            phone_registration_months=48, residential_address="北京市朝阳区望京街道",
            resident_city="北京"),
        device=DeviceInfo(device_id="DEV_0001", fingerprint="fp-a1b2c3", brand="Apple",
                          model="iPhone 15", os="iOS 17", ip="114.240.1.10", ip_city="北京",
                          first_seen_days=600, account_count=2, sim_count=1,
                          resident_city="北京"),
        loan_request=LoanRequest(amount=150000, term_months=12, purpose=LoanPurpose.CONSUMPTION),
        documents=_docs("APP20260001", *all_docs),
    )

    # 案例2: 欺诈团伙（黑名单+多头+设备风险+团伙关联）-> 自动拒绝
    fraud = LoanApplication(
        application_id="APP20260002", channel=Channel.H5,
        applicant=Applicant(
            applicant_id="CUST-002", name="陈强", id_number="440305199003084321",
            phone="13700000002", birthday=date(1990, 3, 8), gender="男",
            education=Education.COLLEGE, marital_status=MaritalStatus.DIVORCED,
            housing_status=HousingStatus.RENTED, employment_type=EmploymentType.SELF_EMPLOYED,
            employer="陈氏贸易商行", job_title="经营者", work_years=5,
            monthly_income=30000, income_evidence=IncomeEvidence.SELF_REPORTED,
            phone_registration_months=2, residential_address="广东省深圳市福田区",
            resident_city="深圳"),
        device=DeviceInfo(device_id="DEV_FRAUD_X", fingerprint="fp-evil-001", brand="未知",
                          model="Android模拟器", os="Android 13", is_rooted=True,
                          is_emulator=True, is_proxy=True, ip="103.45.6.7", ip_city="境外",
                          first_seen_days=1, account_count=6, sim_count=5, resident_city="深圳"),
        loan_request=LoanRequest(amount=200000, term_months=24, purpose=LoanPurpose.CONSUMPTION),
        documents=_docs("APP20260002", *all_docs),
    )

    # 案例3: 高风险客户（多头+新设备+负债高+经营贷缺执照）-> 人工复核
    review = LoanApplication(
        application_id="APP20260003", channel=Channel.APP,
        applicant=Applicant(
            applicant_id="CUST-003", name="张丽", id_number="310104198201234567",
            phone="13600000003", birthday=date(1982, 1, 23), gender="女",
            education=Education.COLLEGE, marital_status=MaritalStatus.DIVORCED,
            housing_status=HousingStatus.RENTED, employment_type=EmploymentType.SELF_EMPLOYED,
            employer="丽华便利店", job_title="经营者", work_years=8,
            monthly_income=18000, income_evidence=IncomeEvidence.SELF_REPORTED,
            phone_registration_months=36, residential_address="上海市闵行区",
            resident_city="上海"),
        device=DeviceInfo(device_id="DEV_0007", fingerprint="fp-g7h8i9", brand="HUAWEI",
                          model="Mate 60", os="HarmonyOS 4", ip="101.86.3.20", ip_city="上海",
                          first_seen_days=30, account_count=2, sim_count=1, resident_city="上海"),
        loan_request=LoanRequest(amount=100000, term_months=12, purpose=LoanPurpose.BUSINESS),
        documents=_docs("APP20260003", *all_docs),
    )

    # 案例4: 资料缺失 -> 补件提示
    supplement = LoanApplication(
        application_id="APP20260004", channel=Channel.APP,
        applicant=Applicant(
            applicant_id="CUST-004", name="刘洋", id_number="110101199701015678",
            phone="13500000004", birthday=date(1997, 1, 1), gender="男",
            education=Education.BACHELOR, marital_status=MaritalStatus.SINGLE,
            housing_status=HousingStatus.RENTED, employment_type=EmploymentType.FULL_TIME,
            employer="南京某科技有限公司", job_title="工程师", work_years=1,
            monthly_income=12000, income_evidence=IncomeEvidence.PAYSLIP,
            phone_registration_months=3, residential_address="江苏省南京市江宁区",
            resident_city="南京"),
        device=DeviceInfo(device_id="DEV_0004", fingerprint="fp-d4e5f6", brand="Xiaomi",
                          model="Redmi K60", os="Android 14", ip="180.101.50.9",
                          ip_city="南京", first_seen_days=200, account_count=1, sim_count=1,
                          resident_city="南京"),
        loan_request=LoanRequest(amount=50000, term_months=12, purpose=LoanPurpose.CONSUMPTION),
        documents=_docs("APP20260004", DocType.ID_FRONT, DocType.ID_BACK, DocType.FACE_VIDEO),
    )

    # 案例5: 审批通过但资金用途违规 -> 合规一票否决
    compliance = LoanApplication(
        application_id="APP20260005", channel=Channel.APP,
        applicant=Applicant(
            applicant_id="CUST-005", name="王强", id_number="320102199207084321",
            phone="13400000005", birthday=date(1992, 7, 8), gender="男",
            education=Education.BACHELOR, marital_status=MaritalStatus.MARRIED,
            housing_status=HousingStatus.MORTGAGED, employment_type=EmploymentType.FULL_TIME,
            employer="杭州某网络公司", job_title="运营经理", work_years=5,
            monthly_income=20000, income_evidence=IncomeEvidence.BANK_FLOW,
            phone_registration_months=60, residential_address="浙江省杭州市西湖区",
            resident_city="杭州"),
        device=DeviceInfo(device_id="DEV_0005", fingerprint="fp-j1k2l3", brand="OPPO",
                          model="Find X6", os="Android 14", ip="115.193.8.6", ip_city="杭州",
                          first_seen_days=500, account_count=2, sim_count=1, resident_city="杭州"),
        loan_request=LoanRequest(amount=100000, term_months=12, purpose=LoanPurpose.OTHER,
                                 memo="拟用于股票投资"),
        documents=_docs("APP20260005", *all_docs),
    )

    return {
        "normal": normal,
        "fraud": fraud,
        "review": review,
        "supplement": supplement,
        "compliance": compliance,
    }


# ============================================================
# 结果展示
# ============================================================
def print_stage(node: str, ctx: PipelineContext) -> None:
    cn_name, field = STAGES.get(node, (node, None))
    print(f"  [{cn_name}]", end=" ")
    if field is None:
        print("受理完成，进入流程编排")
        return
    result = getattr(ctx, field, None)
    if result is None:
        print("(跳过)")
        return
    if field == "final_decision":
        return  # 最终决策单独打印
    print(f"结论={result.status.value:<15} 决策层={result.layer or '-'}")
    if result.summary:
        print(f"      {result.summary}")
    if result.detail:
        print(f"      {result.detail}")


def print_final(ctx: PipelineContext) -> None:
    final = ctx.final_decision
    decision_cn = {"APPROVED": "✅ 审批通过", "REJECTED": "❌ 审批拒绝",
                   "MANUAL_REVIEW": "⏸  人工复核", "SUPPLEMENT": "📋 补件"}[final.decision.value]
    print(THIN)
    print(f"  最终决策: {decision_cn}")
    if final.final_amount and final.final_rate:
        print(f"  额度: {final.final_amount:,.0f} 元 | 年化利率: {final.final_rate}% | "
              f"期限: {final.term_months} 月")
    if final.supplement_notice:
        print(f"  补件提示: {final.supplement_notice}")
    if final.reasons:
        print("  理由链:")
        for r in final.reasons:
            print(f"    • {r}")
    print(f"  合规检查: {'通过' if final.compliance_passed else '不通过'} | "
          f"审计编号: {final.audit_ref}")
    stages = " → ".join(f"{k}:{v}" for k, v in final.stage_results.items())
    print(f"  环节链路: {stages}")


def save_output(ctx: PipelineContext) -> None:
    final = ctx.final_decision
    payload = {
        "final_decision": final.model_dump(mode="json"),
        "audit_events": [e.model_dump(mode="json") for e in ctx.audit_events],
    }
    path = settings.OUTPUT_DIR / f"{final.application_id}_decision.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    print(f"  审计留痕: {path}")


def run_case(name: str, application: LoanApplication) -> PipelineContext:
    print(LINE)
    print(f"案例[{name}] 申请单 {application.application_id} | "
          f"{application.applicant.name} | 申请 {application.loan_request.amount:,.0f}元 "
          f"{application.loan_request.term_months}月 | 用途 {application.loan_request.purpose.value}")
    print(THIN)
    orchestrator = get_orchestrator()
    last_ctx: PipelineContext = PipelineContext(application=application)
    for node, ctx in orchestrator.stream(application):
        print_stage(node, ctx)
        last_ctx = ctx
    print_final(last_ctx)
    save_output(last_ctx)
    return last_ctx


async def run_cases_concurrent(names: list, cases: dict) -> dict:
    """高并发异步模式：所有案例经异步流水线并发处理。"""
    from credit_risk_system.core.async_pipeline import AsyncRiskPipeline
    from credit_risk_system.models.risk import FinalDecision

    pipeline = AsyncRiskPipeline()
    t0 = time.perf_counter()
    print("  异步流水线: 提交 %d 笔申请（并发处理）" % len(names))
    for name in names:
        await pipeline.submit(cases[name])
    # 等待全部完成（并发等待，非串行轮询）
    finals: dict[str, FinalDecision] = {}
    for name in names:
        final = await pipeline.wait(cases[name].application_id)
        finals[name] = final
    print(f"  异步流水线总耗时: {(time.perf_counter() - t0) * 1000:.0f}ms")
    print("  流水线统计:", json.dumps(await pipeline.stats(), ensure_ascii=False))
    return finals


def main() -> None:
    parser = argparse.ArgumentParser(description="信贷自动风控多Agent系统")
    parser.add_argument("--case", default="all",
                        choices=["all", "normal", "fraud", "review", "supplement", "compliance"],
                        help="运行指定演示案例（默认all）")
    parser.add_argument("--concurrent", action="store_true",
                        help="高并发异步模式（asyncio流水线并发处理全部案例）")
    args = parser.parse_args()

    mode = "LLM推理 + 规则引擎（已配置API Key）" if settings.llm_enabled \
        else "规则引擎层（离线兜底模式，配置LLM_API_KEY后启用LLM推理）"
    print(LINE)
    print("  信贷自动风控多Agent系统 (Credit Risk Workbench)")
    print(f"  决策模式: {mode}")
    if args.concurrent:
        print("  执行模式: 高并发异步（LangGraph异步编排 + 事件循环流水线）")
    print(LINE)

    cases = build_demo_cases()
    selected = [args.case] if args.case != "all" else list(cases)

    if args.concurrent:
        finals = asyncio.run(run_cases_concurrent(selected, cases))
        results = {name: final for name, final in finals.items() if final is not None}
        # 并发模式逐案例展示最终决策（各环节进度见SSE/事件流）
        for name, final in results.items():
            print(THIN)
            print(f"  案例[{name}] 申请单 {cases[name].application_id} -> {final.decision.value}")
            if final.reasons:
                for r in final.reasons:
                    print(f"    • {r}")
    else:
        results = {}
        for name in selected:
            results[name] = run_case(name, cases[name]).final_decision

    print(LINE)
    print("  案例汇总:")
    for name, final in results.items():
        print(f"    {name:<12} -> {final.decision.value}")
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
