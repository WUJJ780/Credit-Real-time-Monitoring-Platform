"""面向用户的问答RAGAgent（智能客服助手）。

职责：通过对话理解用户需求，引导完成各项操作——
  1. 信贷产品介绍      —— RAG检索知识库作答（LLM配置后生成式回答）
  2. 利率查询          —— 从risk_strategy配置实时生成（永远与规则引擎一致）
  3. 申请单填写引导    —— 返回导航动作（前端跳转表单页）
  4. 申请进度查询      —— 查询异步流水线实时状态与决策
  5. 补件办理          —— 多轮对话闭环：问单号→查缺失清单→用户确认已补→自动重新受理→回报结果

记忆设计：
  - 短期：每会话保存最近N轮对话历史 + 待办状态机（pending），进程内存储；
  - 长期：知识库（knowledge/*.md）+ 业务数据（申请单/决策，经流水线查询）。

双模式：未配置LLM时走规则意图+模板化回答（离线可用）；
配置LLM后检索结果作为上下文注入生成式回答（回答不编造、以知识库为准）。
"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from credit_risk_system.config import risk_strategy as rs
from credit_risk_system.core.logger import get_logger
from credit_risk_system.core.rag_store import Chunk, get_rag_store
from credit_risk_system.models.loan import Document, DocType

logger = get_logger("rag_agent")

# 资料类型中文名（与DocType枚举值一致）
DOC_NAMES = {d.value for d in DocType}
# 补件确认语句中识别资料名的宽松模式
_DOC_MENTION_RE = re.compile("|".join(sorted(DOC_NAMES, key=len, reverse=True)))
# 申请单号模式（APP/WEB/LOAD等前缀或纯大写字母数字编号）
_APP_ID_RE = re.compile(r"\b[A-Z][A-Z0-9\-]{5,}\b")

_STAGE_CN = {
    "dispatch": "受理", "intake": "进件审核", "antifraud": "反欺诈",
    "credit": "信用评估", "approval": "审批决策", "compliance": "合规审计",
    "postloan": "贷后管理", "finalize": "决策总控",
}
_STATUS_CN = {
    "SUBMITTED": "排队中", "PROCESSING": "处理中", "COMPLETED": "已完成",
    "FAILED": "处理失败", "TIMEOUT": "处理超时", "DUPLICATE": "已受理",
}
_DECISION_CN = {
    "APPROVED": "✅ 审批通过", "REJECTED": "❌ 审批拒绝",
    "MANUAL_REVIEW": "⏸ 转人工复核", "SUPPLEMENT": "📋 需补件",
}

_DEFAULT_SUGGESTIONS = ["产品介绍", "利率查询", "如何申请", "办理补件", "查询进度"]

# LLM生成式回答的系统提示词（仅注入检索上下文，禁止编造）
_LLM_SYSTEM = (
    "你是「信贷自动风控系统」的智能客服助手，面向贷款申请人提供咨询与操作引导。"
    "回答规则：\n"
    "1. 仅依据下方【知识库内容】回答，不得编造知识库中不存在的信息；\n"
    "2. 回答简洁友好，中文，必要时分行；\n"
    "3. 涉及利率/额度/条件等数字，必须与知识库一致；\n"
    "4. 如果知识库内容无法回答问题，请明确说明并建议用户转人工咨询。"
)

# 每个会话保留的对话轮数（短期记忆窗口）
_HISTORY_LIMIT = 10
# 会话上限（超出清理最旧会话）
_SESSION_LIMIT = 500


class RAGAgent:
    """面向用户的问答RAGAgent。"""

    def __init__(self, pipeline=None):
        self.store = get_rag_store()
        self.pipeline = pipeline
        # session_id -> {history: [(user, assistant)], pending: dict|None, updated_at: float}
        self.sessions: Dict[str, dict] = {}

    # ============================================================
    # 主入口
    # ============================================================
    async def chat(self, session_id: str, message: str) -> dict:
        """处理一轮对话，返回 {reply, action, suggestions, hint}。

        hint为多轮状态机提示（如"等待申请单号/等待补件确认"），
        供前端展示当前对话所处的交互状态；无待办时为None。
        """
        message = (message or "").strip()
        session = self._session(session_id)
        self._evict()

        if not message:
            return self._reply(session, "您想了解什么？可以直接问我产品、利率、申请、进度或补件问题。")

        session["history"].append({"role": "user", "text": message, "ts": time.time()})
        reply_data = await self._dispatch(session, message)
        reply_data["hint"] = self._pending_hint(session)
        session["history"].append(
            {"role": "assistant", "text": reply_data["reply"], "ts": time.time()})
        return reply_data

    def get_history(self, session_id: str) -> list:
        """获取会话历史（多轮对话跨页面刷新恢复）。"""
        session = self.sessions.get(session_id)
        return list(session["history"]) if session else []

    # ============================================================
    # 意图路由
    # ============================================================
    async def _dispatch(self, session: dict, message: str) -> dict:
        pending = session.get("pending")
        # 多轮状态机优先：等待单号 / 等待补件确认
        if pending and pending["action"] == "supplement_app_id":
            return await self._handle_app_id(session, message, "supplement")
        if pending and pending["action"] == "status_app_id":
            return await self._handle_app_id(session, message, "status")
        if pending and pending["action"] == "supplement_confirm":
            return await self._handle_supplement_confirm(session, message)

        if re.search(r"^(谢谢|感谢|再见|拜拜|好的|嗯嗯?|ok)\b", message, re.I):
            session.pop("pending", None)
            return self._reply(session, "不客气！随时可以问我产品、利率、申请或补件相关问题 😊")
        if re.search(r"补件|补充资料|缺.{0,6}(资料|证明|执照|影像)|重新提交|材料不齐|已上传|补齐|补上了", message):
            return await self._handle_supplement(session, message)
        if re.search(r"利率|利息|年化|费率|lpr|多少点|几厘", message, re.I):
            return self._handle_rate(session)
        if re.search(r"进度|状态|审批结果|到哪一步|怎么样了|通过了吗|结果", message):
            return await self._handle_status(session, message)
        # 产品意图在申请意图之前：「有什么贷款产品」含"贷款"二字，
        # 若先匹配申请规则会被误路由
        if re.search(r"产品|介绍|有什么.{0,4}(贷|产品)|额度|期限|还款方式|提前还", message):
            return await self._handle_product(session, message)
        if re.search(r"申请|借款|贷款|借钱|怎么贷|如何办", message):
            return self._handle_apply(session)
        if re.search(r"能做什么|帮助|你会什么|功能|hello|你好|您好", message, re.I):
            return self._handle_help(session)
        # 兜底：RAG检索
        return await self._handle_fallback(session, message)

    # ============================================================
    # 各意图处理
    # ============================================================
    def _handle_rate(self, session: dict) -> dict:
        """利率查询：从风险策略配置实时生成，保证与规则引擎口径一致。"""
        rows = "\n".join(
            f"• {g}级 +{rs.GRADE_SPREADS[g]:.1f}% → 年化 {rs.LPR_1Y + rs.GRADE_SPREADS[g]:.2f}%"
            for g in ("A", "B", "C", "D"))
        cap_lpr = rs.LPR_1Y * rs.RATE_CAP_LPR_MULTIPLE
        reply = (
            f"当前利率定价（1年期LPR {rs.LPR_1Y}% + 信用等级加点）：\n{rows}\n"
            f"（E级不予准入）\n\n"
            f"• 合规上限：不超过4倍LPR（{cap_lpr}%）与 {rs.RATE_CAP_ABSOLUTE:.0f}% 双线\n"
            f"• 除利息外无任何费用，支持提前结清\n"
            f"• 利率由系统按信用评估等级自动定价，提交申请后即可看到您的专属利率"
        )
        return self._reply(session, reply, suggestions=["如何申请", "产品介绍"])

    async def _handle_product(self, session: dict, message: str) -> dict:
        """产品介绍：RAG检索知识库（LLM配置后生成式回答）。"""
        chunks = [c for _, c in self.store.retrieve(message, top_k=2)]
        if not chunks:
            return self._handle_help(session)
        answer = await self._llm_answer(session, message, chunks)
        if answer:
            return self._reply(session, answer, suggestions=["利率查询", "如何申请"])
        return self._reply(session, self._format_chunks(chunks),
                           suggestions=["利率查询", "如何申请"])

    def _handle_apply(self, session: dict) -> dict:
        """申请引导：返回导航动作（前端跳转申请单填写页）。"""
        reply = (
            "办理贷款很简单，全程线上自动审批：\n\n"
            "1. 填写申请单（个人资料 + 借款金额/期限/用途 + 影像资料）\n"
            "2. 提交后系统秒级完成：进件审核 → 反欺诈 → 信用评估 → 审批决策\n"
            "3. 自动通过即时放款；有疑问随时回来问我\n\n"
            "⚠️ 演示提示：勾选「使用演示身份」可体验完整审批通过流程。"
        )
        return self._reply(session, reply,
                           action={"type": "navigate", "url": "/submit",
                                   "label": "📝 前往填写申请单"},
                           suggestions=["利率查询", "产品介绍"])

    async def _handle_status(self, session: dict, message: str) -> dict:
        """进度查询：从消息提取单号，查询异步流水线。"""
        app_id = _extract_app_id(message)
        if not app_id:
            session["pending"] = {"action": "status_app_id"}
            return self._reply(session, "请告诉我您的申请单号（如 APP20260001）～")
        session.pop("pending", None)
        return await self._query_status(session, app_id)

    async def _handle_app_id(self, session: dict, message: str, flow: str) -> dict:
        """多轮状态机：等待用户提供申请单号。"""
        app_id = _extract_app_id(message)
        if not app_id:
            return self._reply(session, "没有识别到申请单号，请再发一次（如 APP20260004）～")
        session.pop("pending", None)
        if flow == "supplement":
            return await self._query_supplement(session, app_id)
        return await self._query_status(session, app_id)

    async def _handle_supplement(self, session: dict, message: str) -> dict:
        """补件入口：提取单号或询问。"""
        app_id = _extract_app_id(message)
        if app_id:
            return await self._query_supplement(session, app_id)
        session["pending"] = {"action": "supplement_app_id"}
        return self._reply(session,
                           "请告诉我需要补件的申请单号（如 APP20260004），我来帮您查缺失清单～",
                           suggestions=["APP20260004"])

    async def _handle_supplement_confirm(self, session: dict, message: str) -> dict:
        """多轮状态机：用户确认已补资料 → 自动重新受理 → 回报结果。"""
        pending = session.pop("pending", None) or {}
        app_id = pending.get("app_id", "")
        missing = pending.get("missing", [])
        original = self.pipeline.get_application(app_id) if self.pipeline else None
        if original is None:
            return self._reply(session, f"未找到申请单 {app_id}，无法办理补件。")

        mentioned = set(_DOC_MENTION_RE.findall(message))
        if not mentioned:
            return self._reply(session, f"请确认补充了哪些资料，例如回复「已上传{'、'.join(missing)}」～")

        # 在原始资料基础上追加用户确认补充的资料
        docs = list(original.documents)
        existing = {d.doc_type.value for d in docs}
        added = []
        for name in mentioned:
            if name not in existing:
                docs.append(Document(doc_type=name, file_key=f"supplement/{name}.jpg"))
                added.append(name)
        still_missing = [m for m in missing if m not in existing and m not in mentioned]

        if still_missing:
            session["pending"] = {"action": "supplement_confirm",
                                  "app_id": app_id, "missing": still_missing}
            return self._reply(session, f"已记录：{('、'.join(added) or '—')}。"
                                        f"还缺：{'、'.join(still_missing)}，补齐后回复我～")

        # 资料齐全：重新受理审核
        updated = original.model_copy(deep=True)
        updated.documents = docs
        await self.pipeline.resubmit(updated)
        final = await self.pipeline.wait(app_id, timeout=120)
        if final is None:
            return self._reply(session, f"申请单 {app_id} 已重新受理，正在审核中，"
                                        f"稍后回复「查询进度 {app_id}」可查看结果。")
        dcn = _DECISION_CN.get(final.decision.value, final.decision.value)
        reasons = "\n".join(f"• {r}" for r in (final.reasons or ["—"]))
        return self._reply(session,
                           f"✅ 补件办理完成！申请单 {app_id} 已重新受理审核，最新结果：\n\n"
                           f"{dcn}\n{reasons}",
                           suggestions=["查询进度 " + app_id, "利率查询"])

    async def _query_supplement(self, session: dict, app_id: str) -> dict:
        """查询补件状态：SUPPLEMENT则列出缺失清单并进入确认状态机。"""
        if self.pipeline is None:
            return self._reply(session, "服务未连接，暂时无法查询补件状态。")
        status = await self.pipeline.get_status(app_id)
        if status is None:
            return self._reply(session, f"未找到申请单 {app_id}，请核对单号后重试。")
        decision = (status.get("final_decision") or {}).get("decision")
        notice = (status.get("final_decision") or {}).get("supplement_notice") or ""
        if decision != "SUPPLEMENT":
            return self._reply(session,
                               f"申请单 {app_id} 当前状态：{_STATUS_CN.get(status['status'], status['status'])}"
                               f"{'，决策：' + _DECISION_CN.get(decision, decision) if decision else ''}，无需补件～")
        # 从补件提示中解析缺失资料清单
        missing = [m for m in _DOC_MENTION_RE.findall(notice) if m in DOC_NAMES] or ["资料"]
        session["pending"] = {"action": "supplement_confirm",
                              "app_id": app_id, "missing": missing}
        return self._reply(session,
                           f"查到啦！申请单 {app_id} 需要补件：\n\n"
                           f"缺失资料：{'、'.join(missing)}\n\n"
                           f"补齐后回复我，例如「已上传{'、'.join(missing)}」，"
                           f"我会自动帮您重新受理审核～",
                           suggestions=[f"已上传{'、'.join(missing)}"])

    async def _query_status(self, session: dict, app_id: str) -> dict:
        """查询申请进度与决策。"""
        if self.pipeline is None:
            return self._reply(session, "服务未连接，暂时无法查询进度。")
        status = await self.pipeline.get_status(app_id)
        if status is None:
            return self._reply(session, f"未找到申请单 {app_id}，请核对单号后重试。")
        stage = _STAGE_CN.get(status.get("stage", ""), status.get("stage", ""))
        reply = (f"申请单 {app_id}：\n"
                 f"• 状态：{_STATUS_CN.get(status['status'], status['status'])}\n"
                 f"• 当前环节：{stage}（耗时 {status['elapsed_ms']}ms）")
        fd = status.get("final_decision")
        if fd:
            dcn = _DECISION_CN.get(fd.get("decision"), fd.get("decision"))
            reply += f"\n• 最终决策：{dcn}"
            if fd.get("final_amount"):
                reply += f"\n• 额度：{fd['final_amount']:,.0f} 元"
            if fd.get("final_rate"):
                reply += f" | 年化：{fd['final_rate']}%"
            reasons = fd.get("reasons") or []
            if reasons:
                reply += "\n• 理由：" + reasons[0] + ("…" if len(reasons) > 1 else "")
        return self._reply(session, reply,
                           suggestions=["办理补件 " + app_id] if fd and fd.get("decision") == "SUPPLEMENT"
                           else ["产品介绍", "利率查询"])

    def _handle_help(self, session: dict) -> dict:
        reply = ("您好，我是信贷智能助手 🤖，可以帮您：\n\n"
                 "1. 💰 利率查询 —— 各信用等级利率与合规上限\n"
                 "2. 📦 产品介绍 —— 随心贷/生意贷的额度、期限、要求\n"
                 "3. 📝 如何申请 —— 引导填写申请单\n"
                 "4. 📊 查询进度 —— 报申请单号即可实时查询\n"
                 "5. 📋 办理补件 —— 报申请单号，我帮您补资料重新受理\n\n"
                 "直接告诉我您的需求即可～")
        return self._reply(session, reply, suggestions=_DEFAULT_SUGGESTIONS)

    async def _handle_fallback(self, session: dict, message: str) -> dict:
        """兜底：RAG检索作答（LLM配置后生成式回答）。"""
        chunks = [c for _, c in self.store.retrieve(message, top_k=2)]
        if not chunks:
            return self._handle_help(session)
        answer = await self._llm_answer(session, message, chunks)
        if answer:
            return self._reply(session, answer, suggestions=_DEFAULT_SUGGESTIONS)
        return self._reply(session, self._format_chunks(chunks),
                           suggestions=_DEFAULT_SUGGESTIONS)

    # ============================================================
    # LLM生成式回答（可选升级）
    # ============================================================
    async def _llm_answer(self, session: dict, message: str,
                          chunks: List[Chunk]) -> Optional[str]:
        """LLM配置时以检索上下文生成回答；未配置或失败返回None（模板兜底）。"""
        try:
            from credit_risk_system.core.llm_factory import get_llm, get_llm_semaphore
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = get_llm()
            sem = get_llm_semaphore()
            if llm is None or sem is None:
                return None
            context = "\n\n".join(f"【{c.title}】\n{c.content}" for c in chunks)
            history = "\n".join(
                f"{'用户' if m['role'] == 'user' else '助手'}：{m['text']}"
                for m in session["history"][-6:-1])
            system = _LLM_SYSTEM + f"\n\n【知识库内容】\n{context}"
            user = f"【历史对话】\n{history or '（无）'}\n\n【用户当前提问】\n{message}"
            async with sem:
                resp = await llm.ainvoke([SystemMessage(content=system),
                                          HumanMessage(content=user)])
            text = (resp.content or "").strip()
            return text or None
        except Exception as e:  # LLM异常不影响服务，降级模板回答
            logger.warning("RAG生成式回答失败(%s)，降级知识库原文", e)
            return None

    @staticmethod
    def _format_chunks(chunks: List[Chunk]) -> str:
        """离线模板回答：输出知识库切片原文（摘要）。"""
        parts = []
        for c in chunks:
            parts.append(f"📖 根据知识库「{c.title}」：\n{c.brief()}")
        return "\n\n".join(parts)

    # ============================================================
    # 会话管理（短期记忆）
    # ============================================================
    def _session(self, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        if session is None:
            session = {"history": [], "pending": None, "updated_at": time.time()}
            self.sessions[session_id] = session
        session["updated_at"] = time.time()
        if len(session["history"]) > _HISTORY_LIMIT * 2:
            session["history"] = session["history"][-_HISTORY_LIMIT * 2:]
        return session

    def _evict(self) -> None:
        """会话数超限时清理最久未活跃的会话（长期记忆的容量治理）。"""
        if len(self.sessions) <= _SESSION_LIMIT:
            return
        stale = sorted(self.sessions, key=lambda k: self.sessions[k]["updated_at"])
        for sid in stale[: len(self.sessions) - _SESSION_LIMIT]:
            del self.sessions[sid]

    @staticmethod
    def _reply(session: dict, reply: str, *,
               action: Optional[dict] = None,
               suggestions: Optional[List[str]] = None) -> dict:
        return {"reply": reply, "action": action,
                "suggestions": suggestions or []}

    @staticmethod
    def _pending_hint(session: dict) -> Optional[str]:
        """多轮状态机提示：告诉前端当前对话在等用户提供什么。"""
        pending = session.get("pending")
        if not pending:
            return None
        if pending["action"] in ("supplement_app_id", "status_app_id"):
            return "⏳ 等待您提供申请单号"
        if pending["action"] == "supplement_confirm":
            missing = pending.get("missing") or []
            return "⏳ 等待补件确认（还缺：" + "、".join(missing) + "）"
        return None


def _extract_app_id(message: str) -> Optional[str]:
    """从消息中提取申请单号（大写字母开头+数字/连字符，至少6位）。"""
    m = _APP_ID_RE.search(message)
    return m.group(0) if m else None


# 单例
_agent: Optional[RAGAgent] = None


def get_rag_agent(pipeline=None) -> RAGAgent:
    global _agent
    if _agent is None:
        _agent = RAGAgent(pipeline=pipeline)
    return _agent
