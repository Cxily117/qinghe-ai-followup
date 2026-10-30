"""提示词相关 DTO。"""
from pydantic import BaseModel, Field


class PromptSaveIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000, description="提示词正文")
    note: str = Field(default="", max_length=200, description="本次修改说明")


class PromptRollbackIn(BaseModel):
    version: int = Field(ge=0, description="要回滚到的版本号")


class PromptPreviewIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000, description="用草稿提示词试答的问题")
    content: str = Field(min_length=1, max_length=8000, description="尚未保存的草稿正文")
