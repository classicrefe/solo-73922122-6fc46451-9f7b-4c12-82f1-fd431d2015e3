"""街道更名佐证核查 API。

背景：一条街道更名记载即使列了几个出处，这些出处也可能都转引自同一份原件。
本服务按（旧名、新名、生效日）归档更名事项，登记原件档号与转引条目及其引用关系，
并从指定条目发起核查：若存在两条除起点外互不重合、分别通向不同档号在册原件的
引用链，则佐证成立并保存两条链，否则记为佐证不足。
"""
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import (DOC_REGISTERED, DOC_WITHDRAWN, ENTRY_ACTIVE,
                     ENTRY_WITHDRAWN, V_CONFIRMED, V_INFLIGHT, V_INSUFFICIENT,
                     V_RETURNED, V_STALE, Citation, Document, Entry, Matter,
                     Verification)
from .schemas import (CitationsUpdate, CitationTarget, DocumentCreate,
                      EntryCreate, MatterCreate, VerificationCreate)
from .verify import run_verification

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="街道更名佐证核查 API",
    description=__doc__,
    version="1.0.0",
)

try:
    app.state.verify_delay = float(os.getenv("VERIFY_DELAY_SECONDS", "0.2"))
except ValueError:
    app.state.verify_delay = 0.2


def get_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@contextmanager
def txn(session):
    """资料写入事务：任一步失败即回滚，沿用原资料和结论。"""
    try:
        yield
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="资料写入失败，已沿用原资料和结论")


# ---------- 输出序列化 ----------

def matter_out(m: Matter):
    return {
        "id": m.id,
        "old_name": m.old_name,
        "new_name": m.new_name,
        "effective_date": m.effective_date.isoformat() if m.effective_date else None,
        "version": m.version,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def document_out(d: Document):
    return {
        "id": d.id,
        "matter_id": d.matter_id,
        "archive_no": d.archive_no,
        "status": d.status,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def entry_out(session: Session, e: Entry):
    cites = session.query(Citation).filter(Citation.from_entry_id == e.id).all()
    doc_ids = [c.to_document_id for c in cites if c.to_document_id is not None]
    docs = (
        {d.id: d for d in session.query(Document).filter(Document.id.in_(doc_ids))}
        if doc_ids else {}
    )
    items = []
    for c in cites:
        if c.to_entry_id is not None:
            items.append({"type": "entry", "entry_id": c.to_entry_id})
        else:
            d = docs.get(c.to_document_id)
            items.append({
                "type": "document",
                "document_id": c.to_document_id,
                "archive_no": d.archive_no if d else None,
            })
    return {
        "id": e.id,
        "matter_id": e.matter_id,
        "label": e.label,
        "status": e.status,
        "citations": items,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def verification_out(v: Verification):
    return {
        "id": v.id,
        "matter_id": v.matter_id,
        "start_entry_id": v.start_entry_id,
        "status": v.status,
        "chain_a": json.loads(v.chain_a) if v.chain_a else None,
        "chain_b": json.loads(v.chain_b) if v.chain_b else None,
        "matter_version": v.matter_version,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


# ---------- 内部辅助 ----------

def get_matter_or_404(session: Session, matter_id: int) -> Matter:
    m = session.get(Matter, matter_id)
    if m is None:
        raise HTTPException(status_code=404, detail="事项不存在")
    return m


def mark_matter_changed(session: Session, matter_id: int):
    """本事项资料变动（同事务内）：版本+1；旧结论置为待复核；在途核查退回。"""
    now = datetime.utcnow()
    session.query(Matter).filter(Matter.id == matter_id).update(
        {"version": Matter.version + 1}, synchronize_session=False
    )
    session.query(Verification).filter(
        Verification.matter_id == matter_id,
        Verification.status.in_([V_CONFIRMED, V_INSUFFICIENT]),
    ).update({"status": V_STALE, "updated_at": now}, synchronize_session=False)
    session.query(Verification).filter(
        Verification.matter_id == matter_id,
        Verification.status == V_INFLIGHT,
    ).update({"status": V_RETURNED, "updated_at": now}, synchronize_session=False)


def _add_citation(session: Session, matter_id: int, from_entry_id: int,
                  target: CitationTarget) -> Citation:
    """新增一条引用并校验：限于同一事项；条目互引须双方在录；档号须在册。"""
    has_entry = target.entry_id is not None
    has_doc = target.archive_no is not None and target.archive_no.strip() != ""
    if has_entry == has_doc:
        raise HTTPException(status_code=422, detail="每条引用必须且只能指向一个条目或一个档号")
    if has_entry:
        if target.entry_id == from_entry_id:
            raise HTTPException(status_code=422, detail="条目不能引用自身")
        te = session.get(Entry, target.entry_id)
        if te is None or te.matter_id != matter_id:
            raise HTTPException(status_code=422, detail="被引条目不存在或不属于本事项（引用限于同一事项）")
        if te.status != ENTRY_ACTIVE:
            raise HTTPException(status_code=422, detail="被引条目已撤回")
        c = Citation(matter_id=matter_id, from_entry_id=from_entry_id, to_entry_id=te.id)
    else:
        doc = session.query(Document).filter(
            Document.matter_id == matter_id,
            Document.archive_no == target.archive_no.strip(),
        ).first()
        if doc is None:
            raise HTTPException(status_code=422, detail="被引档号不存在于本事项（引用限于同一事项）")
        if doc.status != DOC_REGISTERED:
            raise HTTPException(status_code=422, detail="被引档号已撤回")
        c = Citation(matter_id=matter_id, from_entry_id=from_entry_id, to_document_id=doc.id)
    session.add(c)
    session.flush()
    return c


# ---------- 事项 ----------

@app.get("/", summary="服务信息")
def root():
    return {
        "service": "街道更名佐证核查 API",
        "docs": "/docs",
        "endpoints": ["/matters", "/matters/{id}/documents", "/matters/{id}/entries",
                      "/matters/{id}/verifications", "/verifications/{id}"],
    }


@app.post("/matters", status_code=201, summary="新建更名事项（按旧名+新名+生效日归档）")
def create_matter(body: MatterCreate, session: Session = Depends(get_session)):
    with txn(session):
        dup = session.query(Matter).filter(
            Matter.old_name == body.old_name,
            Matter.new_name == body.new_name,
            Matter.effective_date == body.effective_date,
        ).first()
        if dup:
            raise HTTPException(status_code=409, detail="同一旧名、新名、生效日的事项已存在")
        m = Matter(old_name=body.old_name, new_name=body.new_name,
                   effective_date=body.effective_date, version=0)
        session.add(m)
        session.flush()
        return matter_out(m)


@app.get("/matters", summary="列出全部事项")
def list_matters(session: Session = Depends(get_session)):
    return [matter_out(m) for m in session.query(Matter).order_by(Matter.id).all()]


@app.get("/matters/{matter_id}", summary="事项详情（含档号、条目、引用、最近结论）")
def matter_detail(matter_id: int, session: Session = Depends(get_session)):
    m = get_matter_or_404(session, matter_id)
    docs = session.query(Document).filter(Document.matter_id == matter_id).order_by(Document.id).all()
    entries = session.query(Entry).filter(Entry.matter_id == matter_id).order_by(Entry.id).all()
    verifs = (session.query(Verification).filter(Verification.matter_id == matter_id)
              .order_by(Verification.id.desc()).limit(20).all())
    return {
        "matter": matter_out(m),
        "documents": [document_out(d) for d in docs],
        "entries": [entry_out(session, e) for e in entries],
        "verifications": [verification_out(v) for v in verifs],
    }


# ---------- 原件档号 ----------

@app.post("/matters/{matter_id}/documents", status_code=201, summary="登记原件档号（已撤回的档号再次提交视为重新登记）")
def register_document(matter_id: int, body: DocumentCreate, session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    archive_no = body.archive_no.strip()
    with txn(session):
        doc = session.query(Document).filter(
            Document.matter_id == matter_id, Document.archive_no == archive_no
        ).first()
        if doc is not None:
            if doc.status == DOC_REGISTERED:
                raise HTTPException(status_code=409, detail="该档号已在册")
            doc.status = DOC_REGISTERED  # 重新登记
            doc.updated_at = datetime.utcnow()
        else:
            doc = Document(matter_id=matter_id, archive_no=archive_no, status=DOC_REGISTERED)
            session.add(doc)
            session.flush()
        mark_matter_changed(session, matter_id)
        return document_out(doc)


@app.delete("/matters/{matter_id}/documents/{archive_no}", summary="撤回原件档号")
def withdraw_document(matter_id: int, archive_no: str, session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    with txn(session):
        doc = session.query(Document).filter(
            Document.matter_id == matter_id, Document.archive_no == archive_no
        ).first()
        if doc is None or doc.status == DOC_WITHDRAWN:
            raise HTTPException(status_code=404, detail="档号不存在或已撤回")
        doc.status = DOC_WITHDRAWN
        doc.updated_at = datetime.utcnow()
        mark_matter_changed(session, matter_id)
        return document_out(doc)


# ---------- 转引条目与引用 ----------

@app.post("/matters/{matter_id}/entries", status_code=201, summary="登记转引条目（可同时给出引用）")
def add_entry(matter_id: int, body: EntryCreate, session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    with txn(session):
        e = Entry(matter_id=matter_id, label=body.label, status=ENTRY_ACTIVE)
        session.add(e)
        session.flush()
        for t in body.citations:
            _add_citation(session, matter_id, e.id, t)
        mark_matter_changed(session, matter_id)
        return entry_out(session, e)


@app.delete("/matters/{matter_id}/entries/{entry_id}", summary="撤回转引条目")
def withdraw_entry(matter_id: int, entry_id: int, session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    with txn(session):
        e = session.get(Entry, entry_id)
        if e is None or e.matter_id != matter_id or e.status == ENTRY_WITHDRAWN:
            raise HTTPException(status_code=404, detail="条目不存在或已撤回")
        e.status = ENTRY_WITHDRAWN
        e.updated_at = datetime.utcnow()
        mark_matter_changed(session, matter_id)
        return entry_out(session, e)


@app.put("/matters/{matter_id}/entries/{entry_id}/citations", summary="改录引用（整体替换该条目的引用）")
def update_citations(matter_id: int, entry_id: int, body: CitationsUpdate,
                     session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    with txn(session):
        e = session.get(Entry, entry_id)
        if e is None or e.matter_id != matter_id or e.status != ENTRY_ACTIVE:
            raise HTTPException(status_code=404, detail="条目不存在或已撤回")
        session.query(Citation).filter(Citation.from_entry_id == entry_id).delete(
            synchronize_session=False
        )
        for t in body.citations:
            _add_citation(session, matter_id, entry_id, t)
        mark_matter_changed(session, matter_id)
        return entry_out(session, e)


# ---------- 核查 ----------

@app.post("/matters/{matter_id}/verifications", status_code=202,
          summary="按转引条目发起核查（异步；wait=true 可等待结果）")
def start_verification(matter_id: int, body: VerificationCreate, request: Request,
                       wait: bool = Query(False, description="是否等待核查完成后返回"),
                       session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    e = session.get(Entry, body.entry_id)
    if e is None or e.matter_id != matter_id or e.status != ENTRY_ACTIVE:
        raise HTTPException(status_code=404, detail="起点条目不存在或已撤回")
    with txn(session):
        v = Verification(matter_id=matter_id, start_entry_id=e.id, status=V_INFLIGHT)
        session.add(v)
        session.flush()
        vid = v.id
    delay = getattr(request.app.state, "verify_delay", 0.0)
    t = threading.Thread(target=run_verification, args=(vid, delay), daemon=True)
    t.start()
    if wait:
        t.join(timeout=max(10.0, delay + 10.0))
    session.expire_all()
    return verification_out(session.get(Verification, vid))


@app.get("/matters/{matter_id}/verifications", summary="查询本事项的核查结论（可按起点条目、状态过滤）")
def list_verifications(matter_id: int,
                       entry_id: Optional[int] = Query(None, description="按起点条目过滤"),
                       status: Optional[str] = Query(None, description="按状态过滤"),
                       session: Session = Depends(get_session)):
    get_matter_or_404(session, matter_id)
    q = session.query(Verification).filter(Verification.matter_id == matter_id)
    if entry_id is not None:
        q = q.filter(Verification.start_entry_id == entry_id)
    if status:
        q = q.filter(Verification.status == status)
    return [verification_out(v) for v in q.order_by(Verification.id.desc()).all()]


@app.get("/verifications/{verification_id}", summary="查询单次核查的状态与结论")
def get_verification(verification_id: int, session: Session = Depends(get_session)):
    v = session.get(Verification, verification_id)
    if v is None:
        raise HTTPException(status_code=404, detail="核查记录不存在")
    return verification_out(v)
