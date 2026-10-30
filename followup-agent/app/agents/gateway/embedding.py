"""Embedding 客户端（★ 阶段 1 核心模块 ② 的迁移目标）。

契约：embed(texts) -> np.ndarray，形状 (n, dim)，已做 L2 归一化。
      归一化后「内积 = 余弦相似度」，检索侧就不用每算一次除一次范数。

两个实现：
  mock   —— 字符 bigram 哈希的确定性向量，无真实语义，仅用于离线联调与测试；
  openai —— 任何 OpenAI 兼容 /embeddings 接口（智谱 embedding-3 / BGE / bge-m3 等）。

维度守卫：写入库的向量维度必须等于 settings.embed_dim（PG 的 vector(1024) 会硬性拒绝
         维度不符的写入），所以这里在调用后立刻校验，出错时给出可操作的提示。
"""
from __future__ import annotations

import hashlib
import logging

import httpx
import numpy as np

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 单次请求最多送多少条文本（智谱/OpenAI 都有批量上限，32 是安全的公共值）
BATCH_SIZE = 32


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化；零向量保持为零，避免除零警告。"""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype("float32")


class EmbeddingClient:
    """统一向量化入口，业务代码不感知供应商。"""

    def __init__(self, provider: str | None = None, dim: int | None = None) -> None:
        s = get_settings()
        self.provider = (provider or s.embed_provider or "mock").lower()
        self.dim = dim or s.embed_dim
        self.base_url = (s.llm_api_base or "https://api.openai.com/v1").rstrip("/")
        self.api_key = s.llm_api_key
        self.model = s.embed_model
        self.timeout = s.llm_timeout

    async def embed(self, texts: list[str]) -> np.ndarray:
        """批量向量化，返回 L2 归一化后的矩阵。"""
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")

        if self.provider == "openai":
            mat = await self._embed_openai(texts)
        else:
            mat = self._embed_mock(texts)

        mat = _l2_normalize(mat)
        if mat.shape[1] != self.dim:
            raise ValueError(
                f"向量维度不一致：模型返回 {mat.shape[1]} 维，但配置 EMBED_DIM={self.dim}。"
                f"请统一 .env 里的 EMBED_DIM 与数据库向量列定义后重新入库"
            )
        return mat

    async def embed_query(self, text: str) -> np.ndarray:
        """单条查询向量（形状 (dim,)）。"""
        return (await self.embed([text]))[0]

    # ---------------- 真实供应商 ----------------

    async def _embed_openai(self, texts: list[str]) -> np.ndarray:
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY 未配置，无法调用真实 embedding（可设 EMBED_PROVIDER=mock）")

        vectors: list[list[float]] = []
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = texts[start : start + BATCH_SIZE]
                payload: dict = {"model": self.model, "input": batch}
                # 显式指定维度：智谱/OpenAI 的 v3 系列都支持，能对齐 vector(1024)
                payload["dimensions"] = self.dim

                resp = await client.post(
                    f"{self.base_url}/embeddings", headers=headers, json=payload
                )
                if resp.status_code == 400 and "dimensions" in payload:
                    # 少数兼容接口不认 dimensions 参数：去掉重试一次
                    logger.info("上游不支持 dimensions 参数，改用模型默认维度重试")
                    payload.pop("dimensions")
                    resp = await client.post(
                        f"{self.base_url}/embeddings", headers=headers, json=payload
                    )
                resp.raise_for_status()

                data = resp.json()["data"]
                # 按 index 排序，防止上游乱序返回导致切片与向量错位
                data.sort(key=lambda d: d.get("index", 0))
                vectors.extend(d["embedding"] for d in data)

        return np.array(vectors, dtype="float32")

    # ---------------- mock ----------------

    def _embed_mock(self, texts: list[str]) -> np.ndarray:
        """确定性哈希向量：同样文本 → 同样向量，保证测试可复现。"""
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            vec = out[row]
            if len(text) == 1:
                vec[hash(text) % self.dim] += 1.0
                continue
            for i in range(len(text) - 1):
                digest = hashlib.md5(text[i : i + 2].encode("utf-8")).hexdigest()
                vec[int(digest, 16) % self.dim] += 1.0
        return out


_default_client: EmbeddingClient | None = None


def get_embedder() -> EmbeddingClient:
    """进程内复用同一个客户端（无状态，可安全共享）。"""
    global _default_client
    if _default_client is None:
        _default_client = EmbeddingClient()
    return _default_client
