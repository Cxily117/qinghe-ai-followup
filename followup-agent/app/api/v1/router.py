"""v1 路由聚合：新增模块只需在此登记一行。"""
from fastapi import APIRouter

from app.api.v1 import agent, audit, auth, chat, knowledge, prompts, reports, tasks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(agent.router)
api_router.include_router(tasks.router)
api_router.include_router(knowledge.router)
api_router.include_router(prompts.router)
api_router.include_router(audit.router)
api_router.include_router(reports.router)
