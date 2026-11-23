"""ORM 模型：事项（旧名/新名/生效日归档）、原件档号、转引条目、引用、核查结论。"""
from datetime import datetime

from sqlalchemy import (Column, Date, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)

from .database import Base


def utcnow():
    return datetime.utcnow()


# 原件档号状态
DOC_REGISTERED = "在册"
DOC_WITHDRAWN = "已撤回"

# 转引条目状态
ENTRY_ACTIVE = "在录"
ENTRY_WITHDRAWN = "已撤回"

# 核查结论状态
V_INFLIGHT = "在途"        # 核查进行中
V_CONFIRMED = "佐证成立"    # 找到两条除起点外互不重合、通向不同档号在册原件的引用链
V_INSUFFICIENT = "佐证不足"  # 未找到这样的两条链
V_STALE = "待复核"         # 结论作出后本事项资料发生变动
V_RETURNED = "退回"        # 核查在途期间本事项资料发生变动，核查被退回


class Matter(Base):
    """更名事项：按旧名、新名、生效日唯一归档。"""
    __tablename__ = "matters"

    id = Column(Integer, primary_key=True)
    old_name = Column(String(200), nullable=False)
    new_name = Column(String(200), nullable=False)
    effective_date = Column(Date, nullable=False)
    version = Column(Integer, nullable=False, default=0)  # 资料每次变动 +1
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("old_name", "new_name", "effective_date", name="uq_matter_key"),
    )


class Document(Base):
    """原件档号。"""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    matter_id = Column(Integer, ForeignKey("matters.id"), nullable=False, index=True)
    archive_no = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default=DOC_REGISTERED)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("matter_id", "archive_no", name="uq_doc_archive"),
    )


class Entry(Base):
    """转引条目（如某志书、某汇编中的一条记载）。"""
    __tablename__ = "entries"

    id = Column(Integer, primary_key=True)
    matter_id = Column(Integer, ForeignKey("matters.id"), nullable=False, index=True)
    label = Column(String(500), nullable=False)
    status = Column(String(20), nullable=False, default=ENTRY_ACTIVE)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Citation(Base):
    """引用边：某条目转引另一条目，或直接注明原件档号。限于同一事项。"""
    __tablename__ = "citations"

    id = Column(Integer, primary_key=True)
    matter_id = Column(Integer, ForeignKey("matters.id"), nullable=False, index=True)
    from_entry_id = Column(Integer, ForeignKey("entries.id"), nullable=False, index=True)
    to_entry_id = Column(Integer, ForeignKey("entries.id"), nullable=True)
    to_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class Verification(Base):
    """一次核查及其结论。"""
    __tablename__ = "verifications"

    id = Column(Integer, primary_key=True)
    matter_id = Column(Integer, ForeignKey("matters.id"), nullable=False, index=True)
    start_entry_id = Column(Integer, ForeignKey("entries.id"), nullable=False)
    status = Column(String(20), nullable=False, default=V_INFLIGHT)
    chain_a = Column(Text, nullable=True)  # JSON 序列化的引用链
    chain_b = Column(Text, nullable=True)
    matter_version = Column(Integer, nullable=True)  # 结论所依据的资料版本
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
