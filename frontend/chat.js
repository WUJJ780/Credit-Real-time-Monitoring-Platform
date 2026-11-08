/* 智能助手多轮对话页
   多轮能力：
   1. session_id 持久化在 localStorage —— 刷新/重开页面后从服务端拉取历史恢复上下文；
   2. 「新对话」按钮 —— 换新会话重新开始；
   3. 状态机提示条（hint）—— 补件流程中实时显示"正在等您提供什么"；
   4. 消息带时间戳与头像，Markdown轻渲染（加粗/列表）。 */
"use strict";

const $ = (sel) => document.querySelector(sel);

// ---------------- 会话ID（多轮记忆的钥匙） ----------------
function newSessionId() {
  return crypto.randomUUID ? crypto.randomUUID()
    : "s-" + Date.now() + "-" + Math.random().toString(36).slice(2);
}
function getSessionId() {
  let sid = localStorage.getItem("rag_session_id");
  if (!sid) {
    sid = newSessionId();
    localStorage.setItem("rag_session_id", sid);
  }
  return sid;
}
let SESSION_ID = getSessionId();

// ---------------- 轻量 Markdown 渲染 ----------------
function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function mdRender(text) {
  // 先转义，再加粗，再按行解析列表/段落
  const lines = esc(text)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .split("\n");
  let out = [], list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const line = raw.trim();
    const ul = line.match(/^[-•●]\s+(.*)$/);
    const ol = line.match(/^\d+[.、]\s+(.*)$/);
    if (ul || ol) {
      const type = ul ? "ul" : "ol";
      if (list !== type) { closeList(); out.push(`<${type}>`); list = type; }
      out.push(`<li>${ul ? ul[1] : ol[1]}</li>`);
    } else {
      closeList();
      out.push(line || "<br>");
    }
  }
  closeList();
  return out.join("");
}

function fmtTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------------- 消息渲染 ----------------
function appendMsg(role, text, ts, { scroll = true, silent = false } = {}) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `
    <div class="avatar">${role === "user" ? "👤" : "🤖"}</div>
    <div class="msg-body">
      <div class="bubble">${mdRender(text)}</div>
      <div class="msg-time">${fmtTime(ts)}</div>
    </div>`;
  $("#chatLog").appendChild(el);
  if (!silent) scrollToBottom();
  return el.querySelector(".bubble");
}

function appendAction(action) {
  if (action && action.type === "navigate") {
    const el = document.createElement("div");
    el.className = "msg assistant";
    el.innerHTML = `<div class="avatar">🤖</div>
      <div class="msg-body"><div class="bubble">
        <a class="chat-action" href="${esc(action.url)}">${esc(action.label || "前往操作")}</a>
      </div></div>`;
    $("#chatLog").appendChild(el);
    scrollToBottom();
  }
}

function renderSuggestions(list) {
  const box = $("#suggestions");
  box.innerHTML = (list || []).map((s) =>
    `<button class="chip" data-q="${esc(s)}">${esc(s)}</button>`).join("");
  box.querySelectorAll(".chip").forEach((btn) =>
    btn.addEventListener("click", () => sendMessage(btn.dataset.q)));
}

function renderHint(hint) {
  const el = $("#hintPill");
  if (hint) {
    el.textContent = hint;
    el.classList.add("show");
  } else {
    el.classList.remove("show");
  }
}

function scrollToBottom() {
  const log = $("#chatLog");
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}

function showTyping(on) {
  let el = $("#chatLog .msg.typing");
  if (on && !el) {
    el = document.createElement("div");
    el.className = "msg assistant typing";
    el.innerHTML = `<div class="avatar">🤖</div><div class="msg-body"><div class="bubble">
      <span class="dot"></span><span class="dot"></span><span class="dot"></span></div></div>`;
    $("#chatLog").appendChild(el);
    scrollToBottom();
  } else if (!on && el) {
    el.remove();
  }
}

// ---------------- 发送 ----------------
async function sendMessage(text) {
  text = (text || "").trim();
  if (!text || $("#sendBtn").disabled) return;
  appendMsg("user", text, Date.now() / 1000);
  $("#chatInput").value = "";
  autoResize();
  showTyping(true);
  $("#sendBtn").disabled = true;
  try {
    const r = await fetch("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_ID, message: text }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    appendMsg("assistant", data.reply || "…", Date.now() / 1000);
    appendAction(data.action);
    renderSuggestions(data.suggestions);
    renderHint(data.hint || null);
  } catch (err) {
    appendMsg("assistant", `⚠️ 暂时无法连接助手服务：${err.message}`, Date.now() / 1000);
    renderSuggestions(["产品介绍", "利率查询"]);
  } finally {
    showTyping(false);
    $("#sendBtn").disabled = false;
  }
}

// ---------------- 会话管理 ----------------
async function restoreHistory() {
  try {
    const r = await fetch(`/api/v1/chat/history/${encodeURIComponent(SESSION_ID)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (data.history && data.history.length) {
      for (const m of data.history) {
        appendMsg(m.role === "user" ? "user" : "assistant", m.text, m.ts, { silent: true });
      }
      scrollToBottom();
      return true;
    }
  } catch {
    appendMsg("assistant", "⚠️ 无法恢复历史会话（服务未连接），将开始新对话。", Date.now() / 1000);
    return true;   // 服务异常时不再重复打招呼
  }
  return false;
}

function startNewSession() {
  SESSION_ID = newSessionId();
  localStorage.setItem("rag_session_id", SESSION_ID);
  $("#chatLog").innerHTML = "";
  renderSuggestions([]);
  renderHint(null);
  sendMessage("你好");
}

// ---------------- 输入框 ----------------
function autoResize() {
  const ta = $("#chatInput");
  ta.style.height = "auto";
  ta.style.height = Math.min(120, ta.scrollHeight) + "px";
}

// ---------------- 初始化 ----------------
async function init() {
  $("#sendBtn").addEventListener("click", () => sendMessage($("#chatInput").value));
  $("#newSessionBtn").addEventListener("click", startNewSession);
  $("#chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage($("#chatInput").value);
    }
  });
  $("#chatInput").addEventListener("input", autoResize);

  // 多轮恢复：有历史 → 恢复上下文；无历史 → 开场白
  const restored = await restoreHistory();
  if (!restored) sendMessage("你好");
}

document.addEventListener("DOMContentLoaded", init);
