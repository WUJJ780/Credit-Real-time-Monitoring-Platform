/* 贷款申请单填写页 —— 表单收集与提交
   表单字段与 credit_risk_system/models/loan.py 一一对应（枚举使用中文值），
   提交 POST /api/v1/applications 后跳转监控台聚焦跟踪。 */
"use strict";

const $ = (sel) => document.querySelector(sel);

// 演示身份（与Mock三要素核验注册表一致 → 核验通过）
const DEMO_IDENTITY = {
  name: "王小明",
  id_number: "110101199503123456",
  phone: "13800000001",
};

const ID_RE = /^\d{17}[\dXx]$/;
const PHONE_RE = /^1\d{10}$/;

function toast(msg, isError = false) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 3600);
}

// ---------------- 演示身份开关 ----------------
function applyDemoIdentity() {
  const on = $("#demoIdentity").checked;
  for (const [id, value] of [["fName", "name"], ["fIdNumber", "id_number"], ["fPhone", "phone"]]) {
    const el = document.getElementById(id);
    if (on) {
      el.value = DEMO_IDENTITY[value];
      el.readOnly = true;
      el.classList.add("readonly");
    } else {
      el.value = "";
      el.readOnly = false;
      el.classList.remove("readonly");
    }
  }
}

// ---------------- 收集表单 → 申请单JSON ----------------
function collectPayload() {
  const fd = new FormData($("#appForm"));
  const get = (k) => String(fd.get(k) ?? "").trim();
  const getNum = (k, fallback = 0) => {
    const v = Number(get(k));
    return Number.isFinite(v) && v !== 0 ? v : fallback;
  };

  const ts = String(Date.now()).slice(-8);
  const docs = fd.getAll("docs").map((docType, i) => ({
    doc_type: docType,
    file_key: `form/${i}_${docType}.jpg`,
    uploaded: true,
  }));

  return {
    application_id: `WEB${ts}`,
    channel: get("channel") || "APP",
    applicant: {
      applicant_id: `AUTO-CUST-${ts}`,
      name: get("name"),
      id_number: get("id_number"),
      phone: get("phone"),
      birthday: get("birthday") || "1995-03-12",
      gender: get("gender") || "未知",
      education: get("education"),
      marital_status: get("marital_status"),
      housing_status: get("housing_status"),
      employment_type: get("employment_type"),
      employer: get("employer"),
      job_title: get("job_title"),
      work_years: getNum("work_years"),
      monthly_income: getNum("monthly_income", 30000),
      income_evidence: get("income_evidence"),
      phone_registration_months: getNum("phone_registration_months"),
      residential_address: get("residential_address"),
      resident_city: get("resident_city"),
    },
    device: {
      device_id: `FORM-DEV-${ts}`,
      fingerprint: `form-fp-${ts}`,
      brand: get("brand"),
      model: get("model"),
      os: get("os"),
      is_rooted: fd.has("is_rooted"),
      is_emulator: fd.has("is_emulator"),
      is_proxy: fd.has("is_proxy"),
      ip: get("ip"),
      ip_city: get("ip_city"),
      first_seen_days: getNum("first_seen_days", 365),
      account_count: getNum("account_count", 1),
      sim_count: getNum("sim_count", 1),
      resident_city: get("resident_city") || get("ip_city"),
    },
    loan_request: {
      amount: getNum("amount", 150000),
      term_months: getNum("term_months", 12),
      purpose: get("purpose") || "消费",
      memo: get("memo") || null,
    },
    documents: docs,
  };
}

// ---------------- 校验 ----------------
function validate(payload) {
  const problems = [];
  const a = payload.applicant;
  if (!a.name) problems.push("姓名未填写");
  if (!ID_RE.test(a.id_number)) problems.push("身份证号须为18位");
  if (!PHONE_RE.test(a.phone)) problems.push("手机号须为11位数字");
  if (!(a.monthly_income > 0)) problems.push("月收入须大于0");
  const lr = payload.loan_request;
  if (!(lr.amount >= 1000 && lr.amount <= 500000)) problems.push("申请金额须在1,000~500,000元");
  if (!(lr.term_months >= 3 && lr.term_months <= 60)) problems.push("借款期限须在3~60个月");
  if (payload.documents.length === 0) problems.push("请至少勾选一项影像资料");
  return problems;
}

// ---------------- 提交 ----------------
async function submitApplication() {
  const payload = collectPayload();
  const problems = validate(payload);
  if (problems.length) {
    toast("校验未通过：" + problems.join("；"), true);
    return;
  }
  const btn = $("#submitBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const r = await fetch("/api/v1/applications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = Array.isArray(data.detail)
        ? data.detail.map((d) => d.msg).join("；")
        : (data.detail || `HTTP ${r.status}`);
      throw new Error(detail.slice(0, 200));
    }
    toast(`申请单 ${data.application_id} 已受理，正在跳转监控台…`);
    setTimeout(() => { window.location.href = `/?focus=${encodeURIComponent(data.application_id)}`; }, 600);
  } catch (err) {
    toast(`提交失败：${err.message}`, true);
    btn.disabled = false;
    btn.textContent = "提交申请单";
  }
}

// ---------------- 初始化 ----------------
function init() {
  applyDemoIdentity();
  $("#demoIdentity").addEventListener("change", applyDemoIdentity);
  $("#appForm").addEventListener("submit", (e) => {
    e.preventDefault();
    submitApplication();
  });
  $("#resetBtn").addEventListener("click", () => {
    $("#appForm").reset();
    applyDemoIdentity();
    toast("表单已重置");
  });
}

document.addEventListener("DOMContentLoaded", init);
