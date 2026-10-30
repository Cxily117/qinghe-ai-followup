# -*- coding: utf-8 -*-
"""
阶段 1 最小原型：随访制度知识库 RAG 命令行问答（无 Web）
====================================================================

功能：
  1. 读取本地随访制度 Markdown 文件 → 按 500 字切片 → 向量化 → 存入 FAISS
  2. 命令行输入问题 → BM25 + 向量 混合检索 top3 → 拼入提示词 → 调用 LLM 生成回答
  3. LLM 调用封装为独立函数，供应商通过环境变量可配置

依赖安装：
  pip install faiss-cpu numpy rank_bm25 jieba httpx

运行方式：
  # 联调模式（无需任何 API Key，内置 mock 供应商）：
  python rag_cli.py 随访制度示例.md

  # 真实模型模式（OpenAI 兼容接口，含国产模型中转均可）：
  set LLM_PROVIDER=openai  &  set LLM_API_KEY=sk-xxx  &  set LLM_API_BASE=https://xxx/v1
  python rag_cli.py 随访制度示例.md

  # 可选参数：
  --no-rag        跳过检索直接问 LLM（用于对比 RAG 效果）

代码结构说明（★ = 后续阶段会复用的核心模块）：
  ① 文本切分器 chunk_markdown      → 将迁移至 app/services/knowledge_service.py
  ② Embedding 客户端 EmbeddingClient → 将迁移至 app/agents/gateway/embedding.py
  ③ 向量库 VectorStore(FAISS)      → 将迁移至 app/services/vector_store.py
  ④ 混合检索器 HybridRetriever     → 将迁移至 app/agents/rag.py
  ⑤ LLM 网关 llm_chat             → 将迁移至 app/agents/gateway/base.py
  ⑥ 提示词构建 build_prompt        → 将迁移至 app/agents/prompts/
  main() 命令行交互属于阶段 1 临时外壳，阶段 2 会被 FastAPI 路由替换。
"""

import argparse
import hashlib
import os
import re
import sys

import httpx
import numpy as np
import jieba
from rank_bm25 import BM25Okapi

# faiss 可选：Windows/离线环境装不上时自动降级为 numpy 余弦检索，逻辑不变
try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False

# ======================================================================
# 配置区：全部可通过环境变量覆盖，与阶段 2 的 pydantic-settings 对应
# ======================================================================
EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "mock")            # mock | openai
LLM_PROVIDER   = os.getenv("LLM_PROVIDER", "mock")              # mock | openai
API_BASE       = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")  # OpenAI 兼容地址
API_KEY        = os.getenv("LLM_API_KEY", "")                   # 密钥走环境变量，不硬编码
CHAT_MODEL     = os.getenv("LLM_CHAT_MODEL", "gpt-4o-mini")     # 对话模型
EMBED_MODEL    = os.getenv("LLM_EMBED_MODEL", "text-embedding-3-small")  # 向量模型
EMBED_DIM      = int(os.getenv("EMBED_DIM", "256"))             # mock 向量维度

CHUNK_SIZE    = 500   # 每个切片最大字符数（制度类中文文档 400~600 较合适）
CHUNK_OVERLAP = 50    # 相邻切片重叠字符数，避免关键句被切断
TOP_K         = 3     # 混合检索返回条数
ALPHA         = 0.5   # 混合权重：向量得分占比（0~1），BM25 占 (1-ALPHA)


# ======================================================================
# ★ 核心模块 ①：文本切分器
#   迁移目标：app/services/knowledge_service.py
#   设计要点：先按空行/标题切成自然段，短段合并到 ~500 字；超长段滑窗硬切带重叠
# ======================================================================
def chunk_markdown(text: str) -> list[str]:
    """把 markdown 全文切成 <=CHUNK_SIZE 的语义切片，返回切片列表。"""
    # 防御性清理：去掉代码块与图片链接（制度文档一般没有，避免干扰切片长度统计）
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)

    # 按空行切自然段（markdown 的标题/条目天然以空行分隔）
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    buf = ""  # 当前攒块的缓冲区
    for p in paras:
        if len(p) > CHUNK_SIZE:
            # 单段超长：先把缓冲区落盘，再对该段做滑窗硬切（步长 = SIZE - OVERLAP）
            if buf:
                chunks.append(buf)
                buf = ""
            step = CHUNK_SIZE - CHUNK_OVERLAP
            for i in range(0, len(p), step):
                chunks.append(p[i:i + CHUNK_SIZE])
            continue
        # 常规情况：能塞进当前块就合并，塞不下就先落盘再开新块
        if len(buf) + len(p) + 1 <= CHUNK_SIZE:
            buf = f"{buf}\n{p}" if buf else p
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


# ======================================================================
# ★ 核心模块 ②：Embedding 客户端（供应商可配置）
#   迁移目标：app/agents/gateway/embedding.py
#   mock 模式：基于字符 bigram 哈希的确定性向量——【无真实语义】，
#              仅用于在无 API Key 时联调整条管道（切分/检索/融合逻辑）
# ======================================================================
class EmbeddingClient:
    """统一入口 embed(texts) -> np.ndarray，屏蔽底层供应商差异。"""

    def embed(self, texts: list[str]) -> np.ndarray:
        if EMBED_PROVIDER == "openai":
            return self._embed_openai(texts)
        return self._embed_mock(texts)

    def _embed_openai(self, texts: list[str]) -> np.ndarray:
        """调用 OpenAI 兼容 /embeddings 接口；返回 L2 归一化后的向量矩阵。"""
        resp = httpx.post(
            f"{API_BASE}/embeddings",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": EMBED_MODEL, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]                       # 与输入顺序一致
        vecs = np.array([d["embedding"] for d in data], dtype="float32")
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)  # 归一化→内积=余弦

    def _embed_mock(self, texts: list[str]) -> np.ndarray:
        """确定性哈希向量：文本相同则向量相同，保证测试可复现。"""
        out = np.zeros((len(texts), EMBED_DIM), dtype="float32")
        for row, t in enumerate(texts):
            v = out[row]
            for i in range(max(len(t) - 1, 1)):          # 滑动取相邻两字符作 bigram
                h = int(hashlib.md5(t[i:i + 2].encode("utf-8")).hexdigest(), 16)
                v[h % EMBED_DIM] += 1.0                  # 词袋式累加
            n = np.linalg.norm(v)
            if n:
                out[row] = v / n                         # L2 归一化
        return out


# ======================================================================
# ★ 核心模块 ③：向量库（FAISS 封装，含 numpy 降级）
#   迁移目标：app/services/vector_store.py
#   归一化向量 + IndexFlatIP（内积）= 余弦相似度；数据量大后可平滑换 HNSW 索引
# ======================================================================
class VectorStore:
    def __init__(self, dim: int):
        self.dim = dim
        if HAS_FAISS:
            self.index = faiss.IndexFlatIP(dim)          # 内积索引
        self.matrix: np.ndarray | None = None            # numpy 降级时保存全部向量

    def add(self, embeddings: np.ndarray) -> None:
        if HAS_FAISS:
            self.index.add(embeddings)
        else:
            self.matrix = embeddings

    def search(self, query_vec: np.ndarray, k: int) -> np.ndarray:
        """返回全部文档的相似度得分数组（长度=文档总数），便于后续融合。"""
        q = query_vec.reshape(1, -1)
        if HAS_FAISS:
            scores, ids = self.index.search(q, k)        # 取前 k 高分
            full = np.full(self.index.ntotal, -1e9, dtype="float32")
            full[ids[0]] = scores[0]                     # 摊平成全量数组
            return full
        sims = (self.matrix @ q.T).ravel()               # 矩阵乘法=批量余弦
        return sims


# ======================================================================
# ★ 核心模块 ④：BM25 + 向量混合检索器
#   迁移目标：app/agents/rag.py
#   融合策略：两路得分各自 min-max 归一化后加权求和（ALPHA 为向量权重）
#   为什么混合：BM25 抓关键词（如"38.5℃""7 天"），向量抓语义（"发热怎么办"
#   ≈ "体温升高处理"），制度问答两类查询都会出现
# ======================================================================
def tokenize_zh(text: str) -> list[str]:
    """中文分词 + 转小写，供 BM25 使用。"""
    return [w.lower() for w in jieba.lcut(text) if w.strip()]


def _minmax(arr: np.ndarray) -> np.ndarray:
    """min-max 归一化到 0~1；全相等时返回全 0.5，避免除零。"""
    lo, hi = arr.min(), arr.max()
    return (arr - lo) / (hi - lo) if hi > lo else np.full_like(arr, 0.5)


class HybridRetriever:
    def __init__(self, chunks: list[str], embedder: EmbeddingClient):
        self.chunks = chunks
        self.embedder = embedder
        self.bm25 = BM25Okapi([tokenize_zh(c) for c in chunks])   # BM25 索引
        embs = embedder.embed(chunks)                             # 只向量化一次
        self.store = VectorStore(embs.shape[1])
        self.store.add(embs)                                      # 向量入库

    def search(self, query: str, top_k: int = TOP_K, alpha: float = ALPHA):
        """返回 [(下标, bm25得分, 向量得分, 融合得分), ...] 按融合分降序。"""
        bm_scores  = np.asarray(self.bm25.get_scores(tokenize_zh(query)), dtype="float32")
        q_vec      = self.embedder.embed([query])[0]
        vec_scores = self.store.search(q_vec, len(self.chunks)).astype("float32")

        fused = alpha * _minmax(vec_scores) + (1 - alpha) * _minmax(bm_scores)
        order = np.argsort(fused)[::-1][:top_k]          # 融合分降序取 top_k
        return [(int(i), float(bm_scores[i]), float(vec_scores[i]), float(fused[i]))
                for i in order]


# ======================================================================
# ★ 核心模块 ⑤：LLM 网关（独立函数，供应商可配置）
#   迁移目标：app/agents/gateway/base.py
#   设计要点：上层只认 messages->str 契约，换供应商只改本函数/注册表，
#             与阶段 2 的「换模型不改业务代码」验收标准对应
# ======================================================================
def llm_chat(messages: list[dict]) -> str:
    """
    输入 OpenAI 格式 messages：[{"role": "system"|"user", "content": "..."}]
    输出助手回复文本。供应商由环境变量 LLM_PROVIDER 决定。
    """
    if LLM_PROVIDER == "openai":
        resp = httpx.post(
            f"{API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": CHAT_MODEL,
                "messages": messages,
                "temperature": 0.2,   # 制度问答要稳，温度调低
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    if LLM_PROVIDER == "mock":
        # 离线联调：不产生真实回答，把检索到的第一条原文片段回显出来，
        # 用于验证「检索 → 拼提示词 → 调用」整条链路是通的。
        ctx = next((m["content"] for m in messages if m["role"] == "user"), "")
        first_src = ctx.split("【资料1】")[-1].split("】")[-1][:120]
        return f"[mock 模型·仅验证链路] 基于检索资料，与「{first_src}...」相关的制度内容如下（略）。"

    raise ValueError(f"未知 LLM_PROVIDER: {LLM_PROVIDER}")


# ======================================================================
# ★ 核心模块 ⑥：提示词构建
#   迁移目标：app/agents/prompts/policy_qa.md（阶段 2 改为模板文件）
#   护栏写入系统提示词：只依据资料回答、带编号引用、超范围拒答、
#   禁止诊断/用药建议——对应需求清单的硬关卡原则
# ======================================================================
SYSTEM_PROMPT = (
    "你是医院随访制度问答助手。规则：\n"
    "1. 只依据【资料】中的内容回答，禁止编造；\n"
    "2. 回答末尾标注引用编号，如 [1][3]；\n"
    "3. 资料不足以回答时，明确说「该问题超出当前知识库范围」，不要猜测，"
    "且此时不要标注任何引用编号；\n"
    "4. 禁止给出诊断、用药建议；涉及红旗症状时提示立即联系专科医生。"
)


def build_prompt(question: str, hits: list) -> tuple[list[dict], list[str]]:
    """把 top_k 资料块编号拼入提示词，返回 (messages, 来源片段列表)。"""
    blocks, sources = [], []
    for n, (idx, _b, _v, _f) in enumerate(hits, 1):
        blocks.append(f"【资料{n}】{CHUNKS[idx]}")
        sources.append(CHUNKS[idx])
    user_content = "资料如下：\n\n" + "\n\n".join(blocks) + f"\n\n问题：{question}"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}], sources


# ======================================================================
# 阶段 1 临时外壳：命令行交互（阶段 2 被 FastAPI /chat 路由替换，不复用）
# ======================================================================
CHUNKS: list[str] = []   # 全局切片表，检索结果按下标取原文


def main() -> None:
    ap = argparse.ArgumentParser(description="随访制度知识库 RAG 命令行问答（阶段1原型）")
    ap.add_argument("mdfile", help="随访制度 markdown 文件路径")
    ap.add_argument("--no-rag", action="store_true", help="跳过检索，直接问 LLM（对比用）")
    args = ap.parse_args()

    print("=" * 62)
    print(f"向量供应商: {EMBED_PROVIDER} | LLM供应商: {LLM_PROVIDER}"
          f" | FAISS: {'启用' if HAS_FAISS else '未安装(numpy降级)'}")
    print("=" * 62)

    # ---- 步骤 1：读文件 → 切片 → 向量化 → 建索引 ----
    raw = open(args.mdfile, encoding="utf-8").read()
    global CHUNKS
    CHUNKS = chunk_markdown(raw)
    print(f"[1/2] 文档已切片：{len(CHUNKS)} 块（每块<= {CHUNK_SIZE} 字，重叠 {CHUNK_OVERLAP} 字）")

    if args.no_rag:
        retriever = None
        print("[2/2] --no-rag 模式：不建立检索索引")
    else:
        retriever = HybridRetriever(CHUNKS, EmbeddingClient())
        print(f"[2/2] FAISS 向量索引 + BM25 索引构建完成，混合检索 top{TOP_K}"
              f"（向量权重 {ALPHA}）")

    # ---- 步骤 2：命令行问答循环 ----
    print("-" * 62)
    print("输入问题开始提问，输入 q 退出。")
    while True:
        try:
            question = input("\n问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not question:
            continue
        if question.lower() in {"q", "quit", "exit"}:
            print("再见。")
            break

        if retriever is None:                     # 对比模式：裸问 LLM
            messages = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": question}]
            sources = []
        else:                                     # RAG 模式：检索→拼提示词
            hits = retriever.search(question)
            print("\n[检索命中]（BM25分 / 向量分 / 融合分）")
            for rank, (idx, b, v, f) in enumerate(hits, 1):
                preview = CHUNKS[idx][:30].replace("\n", " ")
                print(f"  {rank}. #{idx} {b:5.2f} / {v:5.2f} / {f:5.2f}  {preview}...")
            messages, sources = build_prompt(question, hits)

        answer = llm_chat(messages)               # ★ 调 LLM 网关
        print("\n[回答]")
        print(answer)
        if retriever is not None and sources:
            # 改进点修复：只显示回答中实际被引用 [n] 的片段，而非全部拼入块
            cited = sorted({int(m) for m in re.findall(r"\[(\d+)\]", answer)
                            if 1 <= int(m) <= len(sources)})
            if cited:
                print("\n[引用来源] 回答实际引用：")
                for n in cited:
                    preview = sources[n - 1][:60].replace("\n", " ")
                    print(f"  [{n}] {preview}...")
            else:
                print("\n[引用来源] 回答未标注引用（多为拒答场景，属正常）")


if __name__ == "__main__":
    main()
