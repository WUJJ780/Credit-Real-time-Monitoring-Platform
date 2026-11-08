/* 统一界面框架：侧边导航栏 + 内容区顶部品牌栏
   注入到所有前端页面（监控台/申请单填写/智能助手）：
   - 左侧侧栏：导航项（点击即跳转、当前页高亮）+ API文档 + 服务状态；
   - 右侧内容区顶部：通栏品牌标题（🛡 信贷风控系统）；
   页面只需提供 <aside id="sidebar"> 与 <header id="appHeader"> 占位。 */
"use strict";

const NAV_ITEMS = [
  { href: "/",       icon: "📊", label: "实时监控台" },
  { href: "/submit", icon: "📝", label: "申请单填写" },
  { href: "/chat",   icon: "💬", label: "智能助手" },
];

function isActive(path, href) {
  if (href === "/") return path === "/" || path === "/index.html";
  return path === href;
}

function renderChrome() {
  renderHeader();
  renderSidebar();
}

function renderHeader() {
  const host = document.getElementById("appHeader");
  if (!host) return;
  host.innerHTML = `
    <span class="brand-mark" aria-hidden="true">🛡</span>
    <span class="app-title">信贷风控系统</span>`;
}

function renderSidebar() {
  const host = document.getElementById("sidebar");
  if (!host) return;
  const path = location.pathname;
  const nav = NAV_ITEMS.map((it) => `
    <a class="nav-item ${isActive(path, it.href) ? "active" : ""}" href="${it.href}">
      <span class="nav-icon">${it.icon}</span><span>${it.label}</span>
    </a>`).join("");
  host.innerHTML = `
    <nav class="side-nav">${nav}</nav>
    <a class="nav-item side-doc" href="/docs" target="_blank" rel="noopener">
      <span class="nav-icon">📖</span><span>API 文档</span><span class="nav-ext">↗</span>
    </a>
    <div class="side-footer">
      <div class="side-state" id="sideState">
        <span class="dot"></span><span class="state-text">连接中…</span>
      </div>
      <div class="side-mode" id="sideMode">—</div>
    </div>`;
  refreshHealth();
  setInterval(refreshHealth, 10000);
}

async function refreshHealth() {
  const state = document.getElementById("sideState");
  const mode = document.getElementById("sideMode");
  if (!state || !mode) return;
  try {
    const r = await fetch("/healthz");
    const data = await r.json();
    const ok = data.status === "ok";
    state.classList.toggle("on", ok);
    state.classList.toggle("off", !ok);
    state.querySelector(".state-text").textContent = ok ? "服务正常" : "服务异常";
    mode.textContent = data.mode === "llm"
      ? "🤖 LLM推理 + 规则引擎"
      : "🔧 规则引擎模式（离线兜底）";
  } catch {
    state.classList.add("off");
    state.querySelector(".state-text").textContent = "服务不可达";
    mode.textContent = "—";
  }
}

document.addEventListener("DOMContentLoaded", renderChrome);
