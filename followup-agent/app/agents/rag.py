"""RAG 检索与提示词拼装（★ 阶段 1 核心模块 ④ 的迁移目标）。

与阶段 1 原型的差别：
  原型在内存里建 FAISS 索引；这里是「从数据库取已发布切片 → 现算两路得分」。
  对一个院内试点（几千个切片）完全够用，而且省掉一个必须与业务库保持同步的索引服务。

为什么仍然是「BM25 + 向量」而不是纯向量：
  制度问答里两类查询都很常见——
    「骨科术后随访几次」  → 关键词、数字、条款编号，BM25 更准；
    「发烧到39度怎么办」  → 原文写「体温超过38.5℃」，字面不重合，靠向量语义。
  两路 min-max 归一化后线性加权（alpha 为向量权重），比任何单路都稳。

规模上台阶后的演进路径（本阶段不做，已留好接口）：
  切片数 > 1 万时，先用 pgvector 的 HNSW 索引召回 top50，再对候选做 BM25 重排；
  当前实现保留在 `_score_all` 里，替换这一个函数即可。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache

import jieba
import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.gateway.embedding import get_embedder
from app.agents.prompts import load_prompt
from app.core.config import get_settings
from app.models.kb import KbChunk, KbDocument


@dataclass
class RetrievedChunk:
    """一条检索命中。对外只暴露必要字段，向量本身不外传。"""

    chunk_id: int
    document_id: int
    doc_title: str
    doc_type: str
    chunk_index: int
    content: str
    bm25_score: float = 0.0
    vector_score: float = 0.0
    fused_score: float = 0.0

    @property
    def preview(self) -> str:
        """给前端溯源用的截断预览（完整原文走 /kb/chunks/{id}）。"""
        flat = " ".join(self.content.split())
        return flat[:80] + ("…" if len(flat) > 80 else "")

    def to_citation(self, n: int) -> dict:
        """构造返回给前端的引用对象（n 对应回答里的 [n]）。"""
        return {
            "n": n,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "doc_title": self.doc_title,
            "doc_type": self.doc_type,
            "chunk_index": self.chunk_index,
            "preview": self.preview,
        }


@lru_cache(maxsize=20000)
def _tokenize_cached(text: str) -> tuple[str, ...]:
    """中文分词（带缓存）。

    缓存是必要的：每次检索都要对全部候选切片分词，若不复用，
    几千切片时单次查询会多花几十毫秒到上百毫秒。
    """
    return tuple(w.lower() for w in jieba.lcut(text) if w.strip())


def _minmax(arr: np.ndarray) -> np.ndarray:
    """min-max 归一化到 0~1；全相等时返回 0.5，避免除零。"""
    lo, hi = float(arr.min()), float(arr.max())
    return (arr - lo) / (hi - lo) if hi > lo else np.full_like(arr, 0.5)


async def _load_candidates(db: AsyncSession, limit: int) -> list[tuple]:
    """取所有「已发布」文档的切片（合规要求：草稿态文档绝不进检索）。"""
    stmt = (
        select(
            KbChunk.id,
            KbChunk.document_id,
            KbChunk.chunk_index,
            KbChunk.content,
            KbChunk.embedding,
            KbDocument.title,
            KbDocument.doc_type,
        )
        .join(KbDocument, KbDocument.id == KbChunk.document_id)
        .where(KbDocument.status == "published")
        .order_by(KbChunk.id.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).all())


def _score_all(
    rows: list[tuple], query_vec: np.ndarray, query_tokens: list[str], alpha: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """两路打分 + 归一化融合。CPU 密集，调用方必须放到线程里跑。

    返回 (融合分, BM25 原始分, 向量原始分)。
    """
    contents = [r[3] for r in rows]

    # ---- 第 1 路：BM25 ----
    corpus = [_tokenize_cached(c) for c in contents]
    bm25 = BM25Okapi(corpus)
    bm25_scores = np.asarray(bm25.get_scores(query_tokens), dtype="float32")

    # ---- 第 2 路：向量余弦（入库时已 L2 归一化，内积即余弦）----
    dim = query_vec.shape[0]
    vector_scores = np.zeros(len(rows), dtype="float32")
    for i, row in enumerate(rows):
        emb = row[4]
        # 注意：pgvector 读回来可能是 numpy 数组，不能直接 `if not emb`（会抛歧义错误）
        if emb is None or len(emb) == 0:
            continue  # 未向量化的切片只能靠 BM25 命中
        vec = np.asarray(emb, dtype="float32")
        if vec.shape[0] != dim:
            raise ValueError(
                f"向量维度不一致：库中为 {vec.shape[0]} 维，当前查询为 {dim} 维。"
                f"通常是修改了 EMBED_DIM 或换了 embedding 模型，请重建知识库切片向量"
            )
        vector_scores[i] = float(vec @ query_vec)

    return alpha * _minmax(vector_scores) + (1 - alpha) * _minmax(bm25_scores), bm25_scores, vector_scores


async def retrieve(
    db: AsyncSession,
    query: str,
    top_k: int | None = None,
    alpha: float | None = None,
) -> list[RetrievedChunk]:
    """混合检索：返回按融合分降序的 top_k 切片；知识库为空时返回 []。"""
    s = get_settings()
    top_k = top_k or s.rag_top_k
    alpha = s.rag_alpha if alpha is None else alpha

    rows = await _load_candidates(db, s.rag_max_candidates)
    if not rows:
        return []

    query_vec = await get_embedder().embed_query(query)
    query_tokens = list(_tokenize_cached(query))

    # jieba 分词 + BM25 是纯 CPU 计算，放线程避免阻塞事件循环（流式接口尤其敏感）
    fused, bm25_scores, vector_scores = await asyncio.to_thread(
        _score_all, rows, query_vec, query_tokens, alpha
    )

    order = np.argsort(fused)[::-1][:top_k]
    return [
        RetrievedChunk(
            chunk_id=rows[i][0],
            document_id=rows[i][1],
            chunk_index=rows[i][2],
            content=rows[i][3],
            doc_title=rows[i][5],
            doc_type=rows[i][6],
            bm25_score=round(float(bm25_scores[i]), 4),
            vector_score=round(float(vector_scores[i]), 4),
            fused_score=round(float(fused[i]), 4),
        )
        for i in order
        if fused[i] > 0  # 融合分为 0 说明两路都没沾边，直接丢掉，避免污染提示词
    ]


def build_messages(
    question: str,
    hits: list[RetrievedChunk],
    history: list[dict] | None = None,
) -> tuple[list[dict], str]:
    """拼装 LLM messages，返回 (messages, 提示词版本)。

    多轮对话的处理有一个细节：历史里的 assistant 消息带 [1][2] 这类引用编号，
    但本轮的【资料】编号是重新从 1 开始编的，直接把历史塞进去会让编号错乱。
    所以历史消息里的引用标记要剥掉——历史只提供上下文语义，不提供出处。
    """
    from app.agents.guardrails import strip_citations

    prompt = load_prompt("policy_qa")
    blocks = [f"【资料{n}】{hit.content}" for n, hit in enumerate(hits, 1)]
    user_content = "资料如下：\n\n" + "\n\n".join(blocks) + f"\n\n问题：{question}"

    messages: list[dict] = [{"role": "system", "content": prompt.text}]
    for item in history or []:
        role = item.get("role")
        content = item.get("content") or ""
        if role not in {"user", "assistant"} or not content:
            continue
        if role == "assistant":
            content = strip_citations(content)
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_content})
    return messages, prompt.version
