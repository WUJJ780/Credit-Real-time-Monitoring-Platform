"""信贷业务领域模型（进件申请）。"""
from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# 枚举定义
# ============================================================
class LoanPurpose(str, Enum):
    CONSUMPTION = "消费"
    EDUCATION = "教育"
    MEDICAL = "医疗"
    DECORATION = "装修"
    TRAVEL = "旅游"
    BUSINESS = "经营"
    OTHER = "其他"


class EmploymentType(str, Enum):
    FULL_TIME = "全职"
    SELF_EMPLOYED = "个体经营"
    CONTRACT = "合同工"
    RETIREE = "退休"
    OTHER = "其他"


class Education(str, Enum):
    MASTER_ABOVE = "硕士及以上"
    BACHELOR = "本科"
    COLLEGE = "大专"
    HIGH_SCHOOL = "高中"
    BELOW_HIGH_SCHOOL = "中专及以下"


class MaritalStatus(str, Enum):
    MARRIED = "已婚"
    SINGLE = "未婚"
    DIVORCED = "离异"
    WIDOWED = "丧偶"


class HousingStatus(str, Enum):
    OWNED = "自有"
    MORTGAGED = "按揭"
    RENTED = "租房"
    OTHER = "其他"


class IncomeEvidence(str, Enum):
    BANK_FLOW = "银行流水"
    PAYSLIP = "工资单"
    TAX = "税单"
    SELF_REPORTED = "自报"


class Channel(str, Enum):
    APP = "APP"
    H5 = "H5"
    API = "API网关"
    PARTNER = "渠道合作方"
    OFFLINE = "线下网点"


class DocType(str, Enum):
    ID_FRONT = "身份证正面"
    ID_BACK = "身份证反面"
    INCOME_PROOF = "收入证明"
    RESIDENCE_PROOF = "居住证明"
    BUSINESS_LICENSE = "营业执照"
    FACE_VIDEO = "人脸核身视频"
    OTHER = "其他"


# ============================================================
# 业务模型
# ============================================================
class Applicant(BaseModel):
    """申请人信息。"""
    model_config = ConfigDict(extra="ignore")

    applicant_id: str = Field(description="申请人编号")
    name: str = Field(description="姓名")
    id_number: str = Field(description="身份证号")
    phone: str = Field(description="手机号")
    birthday: date = Field(description="出生日期")
    gender: str = Field(default="未知", description="性别")
    education: Education = Field(description="学历")
    marital_status: MaritalStatus = Field(description="婚姻状况")
    housing_status: HousingStatus = Field(description="住房情况")
    employment_type: EmploymentType = Field(description="雇佣类型")
    employer: str = Field(description="工作单位")
    job_title: str = Field(default="", description="职位")
    work_years: float = Field(default=0, ge=0, description="工作年限")
    monthly_income: float = Field(gt=0, description="月收入（元）")
    income_evidence: IncomeEvidence = Field(description="收入证明方式")
    phone_registration_months: int = Field(default=0, ge=0, description="手机号注册时长（月）")
    residential_address: str = Field(default="", description="居住地址")
    resident_city: str = Field(default="", description="常驻城市")

    def age(self, as_of: Optional[date] = None) -> int:
        ref = as_of or date.today()
        born = self.birthday
        return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


class DeviceInfo(BaseModel):
    """申请设备指纹信息。"""
    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(description="设备唯一标识")
    fingerprint: str = Field(default="", description="设备指纹")
    brand: str = Field(default="", description="品牌")
    model: str = Field(default="", description="型号")
    os: str = Field(default="", description="操作系统")
    is_rooted: bool = Field(default=False, description="是否越狱/ROOT")
    is_emulator: bool = Field(default=False, description="是否模拟器")
    is_proxy: bool = Field(default=False, description="是否代理/VPN")
    ip: str = Field(default="", description="申请IP")
    ip_city: str = Field(default="", description="IP归属城市")
    first_seen_days: int = Field(default=365, ge=0, description="设备首次出现距今（天）")
    account_count: int = Field(default=1, ge=1, description="设备上关联账户数")
    sim_count: int = Field(default=1, ge=1, description="SIM卡数量")
    resident_city: str = Field(default="", description="设备常驻城市")

    @property
    def ip_mismatch(self) -> bool:
        """IP归属地与常驻地不一致。"""
        if not self.ip_city or not self.resident_city:
            return False
        return self.ip_city != self.resident_city


class Document(BaseModel):
    """进件影像资料（存储于MinIO）。"""
    model_config = ConfigDict(extra="ignore")

    doc_type: DocType = Field(description="资料类型")
    file_key: str = Field(description="MinIO对象键")
    uploaded: bool = Field(default=True, description="是否已上传")


class LoanRequest(BaseModel):
    """借款申请信息。"""
    model_config = ConfigDict(extra="ignore")

    amount: float = Field(gt=0, le=5_000_000, description="申请金额（元）")
    term_months: int = Field(ge=3, le=60, description="借款期限（月）")
    purpose: LoanPurpose = Field(description="资金用途")
    memo: Optional[str] = Field(default=None, description="用途说明备注")
    is_repeat_customer: bool = Field(default=False, description="是否存量客户")


class LoanApplication(BaseModel):
    """进件申请（业务接入层进入风控中台的主数据）。"""
    model_config = ConfigDict(extra="ignore")

    application_id: str = Field(description="申请单号")
    channel: Channel = Field(description="进件渠道")
    apply_time: datetime = Field(default_factory=datetime.now, description="申请时间")
    applicant: Applicant = Field(description="申请人信息")
    device: DeviceInfo = Field(description="设备信息")
    loan_request: LoanRequest = Field(description="借款请求")
    documents: List[Document] = Field(default_factory=list, description="影像资料清单")
    privacy_consent: bool = Field(default=True, description="个人信息处理授权（PIPL）")
    credit_authorization: bool = Field(default=True, description="征信查询授权")

    def facts(self) -> dict:
        """将申请转换为规则引擎/评分卡使用的标准化特征字典。"""
        a, d, r = self.applicant, self.device, self.loan_request
        return {
            "application_id": self.application_id,
            "channel": self.channel.value,
            "age": a.age(),
            "income": a.monthly_income,
            "amount": r.amount,
            "term": r.term_months,
            "purpose": r.purpose.value,
            "memo": r.memo or "",
            "phone_months": a.phone_registration_months,
            "education": a.education.value,
            "marital": a.marital_status.value,
            "housing": a.housing_status.value,
            "work_years": a.work_years,
            "income_evidence": a.income_evidence.value,
            "employment_type": a.employment_type.value,
            "consent": self.privacy_consent,
            "authorization": self.credit_authorization,
            "rooted": d.is_rooted,
            "emulator": d.is_emulator,
            "proxy": d.is_proxy,
            "first_seen_days": d.first_seen_days,
            "account_count": d.account_count,
            "sim_count": d.sim_count,
            "ip_mismatch": d.ip_mismatch,
            "device_id": d.device_id,
            "is_repeat_customer": r.is_repeat_customer,
        }
