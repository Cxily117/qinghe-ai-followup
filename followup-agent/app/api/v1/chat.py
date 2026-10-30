"""智能问答路由（占位）：D3–D4 接入 RAG + LLM 流式输出。

当前返回 501，是为了让 OpenAPI 文档先行呈现接口形状，
前端可据此先联调页面骨架。阶段 1 原型见 prototype/rag_cli.py。
"""
from fastapi import APIRouter, HTTPException, status

from app.core.deps import CurrentUser

router = APIRouter(prefix="/chat", tags=["智能问答"])


@router.post("", status_code=status.HTTP_501_NOT_IMPLEMENTED, summary="提问（流式）")
async def chat(user: CurrentUser):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="对话能力将在 D3–D4 实现（RAG 链路已在 prototype/rag_cli.py 验证）",
    )
