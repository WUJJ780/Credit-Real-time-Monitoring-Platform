/* 信贷风控实时监控台 —— 前端逻辑
   数据源：/api/v1/*（FastAPI服务同源托管）
   刷新策略：存在在途申请时1s轮询，空闲时3s；最新一笔申请附加SSE实时推送。 */
"use strict";

// ---------------- 常量与映射 ----------------
const STAGES = [
  ["dispatch", "受理"],
  ["intake", "进件审核"],
  ["antifraud", "反欺诈"],
  ["credit", "信用评估"],
  ["approval", "审批决策"],
  ["compliance", "合规审计"],
  ["postloan", "贷后管理"],
  ["finalize", "决策总控"],
];

const STATUS_META = {
  SUBMITTED:  { label: "排队中",   icon: "🕐", cls: "status-submitted" },
  PROCESSING: { label: "处理中",   icon: "⏳", cls: "status-processing" },
  COMPLETED:  { label: "已完成",   icon: "✓",  cls: "status-completed" },
  FAILED:     { label: "处理失败", icon: "✗",  cls: "status-failed" },
  TIMEOUT:    { label: "处理超时", icon: "⏰", cls: "status-timeout" },
  DUPLICATE:  { label: "重复受理", icon: "↻",  cls: "status-duplicate" },
};

const DECISION_META = {
  APPROVED:      { label: "审批通过", icon: "✅", cls: "approved" },
  REJECTED:      { label: "审批拒绝", icon: "❌", cls: "rejected" },
  MANUAL_REVIEW: { label: "人工复核", icon: "⏸",  cls: "review" },
  SUPPLEMENT:    { label: "补件",     icon: "📋", cls: "supplement" },
};

const DIST_ORDER = ["APPROVED", "REJECTED", "MANUAL_REVIEW", "SUPPLEMENT"];

// ---------------- 状态 ----------------
const appCache = new Map();       // application_id -> payload(JSON)
const expandedIds = new Set();    // 展开详情的申请
const liveStages = new Map();     // SSE实时环节（处理中申请）
let lastWatchedId = null;         // 最近一笔SSE跟踪的申请
let focusHandled = false;         // 表单页跳转聚焦是否已处理
const focusId = new URLSearchParams(location.search).get("focus");

// ---------------- 工具 ----------------
const $ = (sel) => document.querySelector(sel);
const fmtMs = (ms) => ms == null ? "—" : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
const fmtMoney = (v) => v == null ? null : `${v.toLocaleString("zh-CN")} 元`;
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
  toast._t = setTimeout(() => el.classList.remove("show"), 3200);
}

// ---------------- 数据拉取与渲染 ----------------
async function refreshStats() {
  try {
    const r = await fetch("/api/v1/stats");
    if (!r.ok) return;
    const s = await r.json();
    const tiles = [
      ["累计受理", s.submitted, ""],
      ["在途处理", s.in_flight, ""],
      ["排队中", s.queued, ""],
      ["已完成", s.completed, ""],
      ["失败 / 超时", `${s.failed} / ${s.timed_out}`, ""],
      ["平均耗时", s.avg_elapsed_ms, "ms"],
      ["P90 耗时", s.p90_ms, "ms"],
      ["P99 耗时", s.p99_ms, "ms"],
    ];
    $("#kpiGrid").innerHTML = tiles.map(([label, value, unit]) => `
      <div class="kpi">
        <div class="label">${label}</div>
        <div class="value">${value}<small>${unit ? " " + unit : ""}</small></div>
      </div>`).join("");
  } catch { /* 忽略瞬时网络错误，下一轮重试 */ }
}

async function refreshApps() {
  let hasActive = false;
  try {
    const r = await fetch("/api/v1/applications?limit=300");
    if (!r.ok) return hasActive;
    const data = await r.json();
    const seen = new Set();
    const list = data.applications;      // 服务端已按最新在前返回
    for (const app of list) {
      seen.add(app.application_id);
      if (app.status === "SUBMITTED" || app.status === "PROCESSING") hasActive = true;
      const key = JSON.stringify(app);
      if (appCache.get(app.application_id) === key) continue;
      appCache.set(app.application_id, key);
      upsertCard(app);
    }
    // 移除服务端已清理的记录
    for (const id of [...appCache.keys()]) {
      if (!seen.has(id)) {
        appCache.delete(id);
        expandedIds.delete(id);
        document.querySelector(`[data-app="${CSS.escape(id)}"]`)?.remove();
      }
    }
    $("#appsCount").textContent = `共 ${data.total ?? data.count} 笔`;
    renderDist(list);
    renderEmpty(list.length === 0);
    handleFocus();
  } catch { /* 忽略瞬时网络错误 */ }
  return hasActive;
}

// 表单页提交后跳转 ?focus=<id>：自动展开卡片、滚动定位、SSE实时跟踪
function handleFocus() {
  if (!focusId || focusHandled || !appCache.has(focusId)) return;
  focusHandled = true;
  expandedIds.add(focusId);
  upsertCard(JSON.parse(appCache.get(focusId)));
  const el = document.querySelector(`[data-app="${CSS.escape(focusId)}"]`);
  if (el) {
    el.classList.add("flash");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  watchEvents(focusId);
  history.replaceState(null, "", "/");
}

function renderEmpty(empty) {
  let el = $("#appList .empty");
  if (empty) {
    if (!el) {
      el = document.createElement("div");
      el.className = "empty";
      el.textContent = "暂无申请 —— 点击左侧「演示案例」或提交自定义申请单开始演示";
      $("#appList").appendChild(el);
    }
  } else {
    el?.remove();
  }
}

function renderDist(list) {
  const counts = { APPROVED: 0, REJECTED: 0, MANUAL_REVIEW: 0, SUPPLEMENT: 0 };
  let total = 0;
  for (const app of list) {
    const d = app.final_decision?.decision;
    if (d in counts) { counts[d] += 1; total += 1; }
  }
  $("#distTotal").textContent = total ? `（终态 ${total} 笔）` : "";
  const max = Math.max(1, ...Object.values(counts));
  $("#distBars").innerHTML = DIST_ORDER.map((d) => {
    const meta = DECISION_META[d];
    const width = total ? Math.round((counts[d] / max) * 100) : 0;
    return `
      <div class="dist-item">
        <div class="row"><span>${meta.icon} ${meta.label}</span><b>${counts[d]}</b></div>
        <div class="dist-track"><div class="dist-fill ${meta.cls}" style="width:${width}%"></div></div>
      </div>`;
  }).join("");
}

// ---------------- 申请卡片 ----------------
function upsertCard(app) {
  const holder = document.getElementById("appList");
  let card = document.querySelector(`[data-app="${CSS.escape(app.application_id)}"]`);
  if (!card) {
    card = document.createElement("article");
    card.className = "app-card";
    card.dataset.app = app.application_id;
    holder.prepend(card);   // 新受理的排最前
  }
  const isOpen = expandedIds.has(app.application_id);
  card.classList.toggle("open", isOpen);
  card.innerHTML = cardHTML(app, isOpen);
  card.querySelector(".app-head")?.addEventListener("click", () => {
    expandedIds.has(app.application_id)
      ? expandedIds.delete(app.application_id)
      : expandedIds.add(app.application_id);
    card.classList.toggle("open");
    card.querySelector(".detail")?.classList.toggle("open");
    if (!card.querySelector(".detail")) upsertCard(app);   // 展开时补渲染详情
  });
}

function cardHTML(app, withDetail) {
  const id = app.application_id;
  const st = STATUS_META[app.status] || { label: app.status, icon: "●", cls: "status-submitted" };
  const final = app.final_decision;
  const dmeta = final ? DECISION_META[final.decision] : null;

  const stagesHTML = STAGES.map(([key, name]) => {
    let cls = "";
    if (app.status === "SUBMITTED") {
      cls = "";                                       // 全部待执行
    } else if (app.status === "PROCESSING") {
      const idx = STAGES.findIndex(([k]) => k === (liveStages.get(id) || app.stage));
      const cur = STAGES.findIndex(([k]) => k === key);
      if (cur < idx) cls = "done";
      else if (cur === idx) cls = "current";
    } else {
      const results = final?.stage_results || {};
      cls = key in results ? "done" : "skipped";      // 终态：按实际执行链路
    }
    return `<div class="stage ${cls}"><div class="node"></div>
            <div class="name">${name}</div></div>`;
  }).join("");

  let banner = "";
  if (final && dmeta) {
    const metaParts = [];
    if (final.final_amount != null) metaParts.push(`额度 ${fmtMoney(final.final_amount)}`);
    if (final.final_rate != null) metaParts.push(`年化 ${final.final_rate}%`);
    if (final.term_months != null) metaParts.push(`期限 ${final.term_months} 月`);
    if (final.fraud_score != null) metaParts.push(`🛡 反欺诈分 ${final.fraud_score}`);
    if (final.fraud_tags?.length) {
      metaParts.push(`⚠ 反欺诈信号：${final.fraud_tags.map(esc).join("、")}`);
    }
    banner = `
      <div class="decision-banner ${dmeta.cls}">
        <span class="headline">${dmeta.icon} ${dmeta.label}</span>
        ${metaParts.map((m) => `<span class="meta">${m}</span>`).join("")}
        ${final.supplement_notice ? `<span class="meta">${esc(final.supplement_notice)}</span>` : ""}
      </div>`;
  } else if (app.status === "FAILED" || app.status === "TIMEOUT") {
    banner = `<div class="decision-banner rejected">
      <span class="headline">${st.icon} ${st.label}</span>
      <span class="meta">${esc(app.error || "—")}</span></div>`;
  }

  let detail = "";
  if (withDetail && final) {
    const reasons = (final.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
    const chain = Object.entries(final.stage_results || {}).map(([k, v]) =>
      `<span class="chip status-${v.toLowerCase()}">${stageName(k)}：${v}</span>`).join("");
    const audits = (app.audit_events || []).map((e) => `
      <li><span class="audit-meta">${(e.ts || "").replace("T", " ").slice(0, 19)}</span><br>
          <b>${esc(e.agent)}</b> [${esc(e.layer)}] ${esc(e.message)}</li>`).join("");
    detail = `
      <div class="detail">
        <h4>决策理由链</h4>
        <ul class="reason-list">${reasons || "<li>—</li>"}</ul>
        <h4>环节链路</h4>
        <div class="stage-chain">${chain || "—"}</div>
        <h4>审计信息</h4>
        <p class="hint">合规检查：${final.compliance_passed ? "✓ 通过" : "✗ 不通过"}
          ｜ 审计编号：${esc(final.audit_ref || "—")}
          ｜ 决策主体：${esc(final.decided_by || "—")}</p>
        <h4>审计事件时间线（${(app.audit_events || []).length} 条）</h4>
        <ul class="audit-list">${audits || "<li>—</li>"}</ul>
      </div>`;
  } else if (withDetail) {
    detail = `<div class="detail"><p class="hint">处理中，暂无详情。</p></div>`;
  }

  return `
    <div class="app-head">
      <span class="app-id">${esc(id)}</span>
      <span class="badge status ${st.cls}">${st.icon} ${st.label}</span>
      ${dmeta ? `<span class="badge decision decision-${dmeta.cls}">${dmeta.icon} ${dmeta.label}</span>` : ""}
      <span class="spacer"></span>
      <span class="app-elapsed">${fmtMs(app.elapsed_ms)}</span>
      <span class="expand-arrow">▼</span>
    </div>
    <div class="stages">${stagesHTML}</div>
    ${banner}
    ${detail}`;
}

function stageName(key) {
  const hit = STAGES.find(([k]) => k === key);
  return hit ? hit[1] : key;
}

// ---------------- SSE 实时跟踪（最近一笔申请） ----------------
function watchEvents(appId) {
  if (!appId || appId === lastWatchedId) return;
  lastWatchedId = appId;
  const es = new EventSource(`/api/v1/applications/${appId}/events`);
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.event === "stage") {
      liveStages.set(appId, ev.stage);
      const cached = appCache.get(appId);
      if (cached) upsertCard(JSON.parse(cached));   // 即时刷新该卡环节
    } else if (ev.event === "completed" || ev.event === "failed" || ev.event === "timeout") {
      liveStages.delete(appId);
      es.close();
      toast(`${appId} 处理完成`);
    }
  };
  es.onerror = () => { es.close(); liveStages.delete(appId); };
}

// ---------------- 受理操作 ----------------
async function submitDemo(caseName) {
  toast(`正在受理演示案例「${caseName}」…`);
  try {
    const r = await fetch(`/api/v1/demo?cases=${encodeURIComponent(caseName)}`,
                          { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    toast(`已受理 ${data.accepted} 笔申请`);
    watchEvents(data.application_ids?.[0]);
    await refreshApps();
  } catch (err) {
    toast(`受理失败：${err.message}`, true);
  }
}

async function submitBatch() {
  const times = Math.max(1, Math.min(500, parseInt($("#batchTimes").value, 10) || 50));
  const btn = $("#batchBtn");
  btn.disabled = true;
  toast(`正在并发受理 ${times * 5} 笔申请（5类案例 × ${times}）…`);
  try {
    const r = await fetch(`/api/v1/demo?cases=all&times=${times}`, { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    toast(`已并发受理 ${data.accepted} 笔申请，实时监控中`);
    watchEvents(data.application_ids?.[0]);
    await refreshApps();
  } catch (err) {
    toast(`批量受理失败：${err.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

// ---------------- 初始化 ----------------
function init() {
  document.querySelectorAll(".case-btn").forEach((btn) =>
    btn.addEventListener("click", () => submitDemo(btn.dataset.case)));
  $("#batchBtn").addEventListener("click", submitBatch);

  // 自适应轮询：有在途申请1s，空闲3s
  (async function pollLoop() {
    const hasActive = await refreshApps();
    await refreshStats();
    setTimeout(pollLoop, hasActive ? 1000 : 3000);
  })();
}

document.addEventListener("DOMContentLoaded", init);
