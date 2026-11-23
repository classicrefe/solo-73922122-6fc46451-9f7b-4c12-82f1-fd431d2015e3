"""请求体的 Pydantic 模型。"""
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


class MatterCreate(BaseModel):
    old_name: str = Field(min_length=1, max_length=200, description="旧街名")
    new_name: str = Field(min_length=1, max_length=200, description="新街名")
    effective_date: date = Field(description="更名生效日，ISO 格式，如 2026-01-01")


class DocumentCreate(BaseModel):
    archive_no: str = Field(min_length=1, max_length=200, description="原件档号")


class CitationTarget(BaseModel):
    """一条引用：指向同事事的另一条目（entry_id）或同事事的原件档号（archive_no），二者必居其一。"""
    entry_id: Optional[int] = Field(default=None, description="被转引的条目 id（须属同一事项且在录）")
    archive_no: Optional[str] = Field(default=None, description="直接注明的原件档号（须属同一事项且在册）")


class EntryCreate(BaseModel):
    label: str = Field(min_length=1, max_length=500, description="条目名称/出处描述")
    citations: List[CitationTarget] = Field(default_factory=list, description="该条目的引用")


class CitationsUpdate(BaseModel):
    citations: List[CitationTarget] = Field(default_factory=list, description="改录后的全部引用（整体替换）")


class VerificationCreate(BaseModel):
    entry_id: int = Field(description="作为核查起点的转引条目 id")
