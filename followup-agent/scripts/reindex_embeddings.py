"""重建知识库切片向量（换 embedding 模型/维度后必须执行）。

为什么需要它：
    向量库里的 embedding 是「入库当时那个模型」产出的。把 EMBED_PROVIDER 从 mock
    换成真实模型后，查询向量与库存向量不在同一语义空间——余弦相似度等于随机数，
    检索结果会看起来"能跑但引错条文"。唯一的解法是把历史切片重新向量化。

用法：
    python scripts/reindex_embeddings.py            # 只重建 published
    python scripts/reindex_embeddings.py --all      # 连 draft/archived 一起重建
    python scripts/reindex_embeddings.py --dry-run  # 只报告，不写库

实现说明：
    - 直接读 SQLite/PG 的 ORM（走 app.core.database），保证与线上读取路径一致；
    - 按批（embed 内部已分批）一次性提交，失败整体回滚，不会出现"重建了一半"；
    - 写入前校验维度与 EMBED_DIM 一致，避免把 2048 维塞进 vector(1024)。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.agents.gateway.embedding import get_embedder  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models.kb import KbChunk, KbDocument  # noqa: E402


async def main(all_docs: bool, dry_run: bool) -> int:
    s = get_settings()
    print(f"embedding 供应商：{s.embed_provider} / 模型：{s.embed_model} / 维度：{s.embed_dim}")
    if s.embed_provider == "mock":
        print("⚠️ 当前仍是 mock 供应商，重建没有意义；请先在 .env 里设 EMBED_PROVIDER=openai")
        return 2

    async with SessionLocal() as db:
        stmt = (
            select(KbChunk, KbDocument)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .order_by(KbChunk.id)
        )
        if not all_docs:
            stmt = stmt.where(KbDocument.status == "published")
        rows = (await db.execute(stmt)).all()
        if not rows:
            print("没有需要重建的切片")
            return 0

        print(f"待重建切片：{len(rows)} 条（{'全部状态' if all_docs else '仅 published'}）")
        if dry_run:
            for chunk, doc in rows:
                print(f"  [{chunk.id}] {doc.title} #{chunk.chunk_index} ({len(chunk.content)} 字)")
            return 0

        embedder = get_embedder()
        texts = [c.content for c, _ in rows]
        vectors = await embedder.embed(texts)

        if vectors.shape[1] != s.embed_dim:
            print(f"❌ 维度不符：模型返回 {vectors.shape[1]}，配置要求 {s.embed_dim}")
            return 3

        for (chunk, _doc), vec in zip(rows, vectors):
            chunk.embedding = vec.tolist()

        await db.commit()
        print(f"✅ 已重建 {len(rows)} 条切片向量（{vectors.shape[1]} 维）")
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连 draft/archived 一起重建")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.all, args.dry_run)))
