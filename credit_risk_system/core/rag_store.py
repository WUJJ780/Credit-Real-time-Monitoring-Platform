"""RAG知识库：加载 knowledge/*.md，按章节切片并建立字符二元组索引。

设计要点：
  - 零额外依赖、离线可用：中文检索用「字符二元组(bigram)集合」近似语义，
    无需embedding模型/向量库；未来接入Milvus+embedding时可替换retrieve实现；
  - 知识库即 Markdown 文档：业务方直接编辑 knowledge/ 目录即可维护；
  - 检索结果供 RAGAgent 使用：未配置LLM时直接返回知识原文（模板化回答），
    配置LLM后作为上下文注入生成式回答。
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from credit_risk_system.core.logger import get_logger

logger = get_logger("rag_store")

# 知识库目录：项目根/knowledge/
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge"

# 用于清洗文本的字符：保留中英文数字，其余作为分词边界
_CLEAN_RE = re.compile(r"[^一-龥a-zA-Z0-9]+")

# 同义词扩展：查询中出现关键词时，把同义表述一并加入检索
# （口语词 → 知识库用语），显著提升短查询召回率
SYNONYMS = {
    "贷款": ["借款", "放款", "信贷", "随心贷", "生意贷", "消费贷", "经营贷"],
    "产品": ["随心贷", "生意贷", "消费贷", "经营贷"],
    "利率": ["利息", "年化", "定价", "费率", "加点", "LPR"],
    "申请": ["进件", "借款", "办理", "受理"],
    "补件": ["补充", "缺失", "资料不全", "材料"],
    "还款": ["月供", "还贷", "结清"],
}


def _expand_query(query: str) -> str:
    """查询扩展：命中关键词时追加同义词（扩展词以空格连接参与bigram）。"""
    extra = []
    for key, words in SYNONYMS.items():
        if key in query:
            extra.extend(words)
    return f"{query} {' '.join(extra)}" if extra else query


def _bigrams(text: str) -> set:
    """字符二元组集合（中文无空格分词，bigram是轻量可靠的近似）。"""
    cleaned = _CLEAN_RE.sub("", text.lower())
    grams = {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}
    return grams or {cleaned}


@dataclass(frozen=True)
class Chunk:
    """知识切片：标题 + 正文 + 来源文件。"""
    title: str
    content: str
    source: str

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.content}"

    def brief(self, limit: int = 320) -> str:
        """展示用摘要（正文超长时截断）。"""
        body = self.content.strip()
        return body if len(body) <= limit else body[:limit] + "……"


class RagStore:
    """知识库：加载 → 切片 → 索引 → 检索。"""

    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory else KNOWLEDGE_DIR
        self.chunks: List[Chunk] = []
        self._index: List[set] = []          # 与chunks同序的bigram集合
        self._load()

    # ----------------------------------------------------------
    # 加载与切片
    # ----------------------------------------------------------
    def _load(self) -> None:
        if not self.directory.exists():
            logger.warning("知识库目录不存在: %s（RAG检索将返回空）", self.directory)
            return
        for md in sorted(self.directory.glob("*.md")):
            self._load_file(md)
        logger.info("RAG知识库加载完成: %d篇文档 -> %d个知识切片",
                    len(set(c.source for c in self.chunks)), len(self.chunks))

    def _load_file(self, path: Path) -> None:
        """按 Markdown 标题切块：一级标题为文档名（并入每块索引），## 为切片标题。"""
        text = path.read_text(encoding="utf-8")
        current_title = path.stem
        buffer: List[str] = []
        doc_title = ""

        def flush() -> None:
            nonlocal current_title
            content = "\n".join(buffer).strip()
            if content:
                self.chunks.append(Chunk(title=current_title, content=content,
                                         source=path.name))
                # 文档标题（如「信贷产品介绍」）参与索引，提升整篇召回
                self._index.append(_bigrams(f"{doc_title} {current_title} {content}"))
            buffer.clear()

        for line in text.splitlines():
            if line.startswith("# ") and not line.startswith("## "):
                doc_title = line[2:].strip()
                continue
            if line.startswith("## "):
                flush()
                current_title = line[3:].strip()
                continue
            if line.strip():
                buffer.append(line.strip())
        flush()

    # ----------------------------------------------------------
    # 检索
    # ----------------------------------------------------------
    def retrieve(self, query: str, top_k: int = 2,
                 min_score: float = 0.10) -> List[Tuple[float, Chunk]]:
        """检索与query最相关的知识切片，返回[(得分, 切片)]，按得分降序。

        计分：查询覆盖率（查询的bigram中有多少比例被切片命中）+
        标题命中加权（切片标题与查询直接相关时 +0.3）。
        短查询（如「有什么贷款产品」）对长文档，覆盖率计分
        显著优于余弦相似度；低于min_score视为不相关。
        """
        if not self.chunks:
            return []
        q = _bigrams(_expand_query(query))
        base = len(q) or 1
        scored = []
        for grams, chunk in zip(self._index, self.chunks):
            inter = len(q & grams)
            if not inter:
                continue
            cover = inter / base
            if q & _bigrams(chunk.title):      # 标题命中加权
                cover = min(1.0, cover + 0.3)
            if cover >= min_score:
                scored.append((cover, chunk))
        scored.sort(key=lambda x: -x[0])
        return scored[:top_k]

    def search(self, query: str, top_k: int = 2) -> List[Chunk]:
        """检索并返回切片列表（忽略分数）。"""
        return [chunk for _, chunk in self.retrieve(query, top_k)]

    def stats(self) -> Dict[str, int]:
        """知识库统计。"""
        return {"files": len({c.source for c in self.chunks}),
                "chunks": len(self.chunks)}


# 单例
_store: Optional[RagStore] = None


def get_rag_store() -> RagStore:
    global _store
    if _store is None:
        _store = RagStore()
    return _store
