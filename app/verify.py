"""佐证核查核心逻辑：引用链搜索与后台核查任务。

判定规则：从起点条目出发，若存在两条引用链，满足
  1. 除起点外互不重合（中间条目与末端原件均不重叠）；
  2. 分别通向两个不同档号的在册原件；
则佐证成立，保存两条链；否则记为佐证不足。
"""
import json
import time
from datetime import datetime

from sqlalchemy import update

from .database import SessionLocal
from .models import (DOC_REGISTERED, ENTRY_ACTIVE, V_CONFIRMED, V_INFLIGHT,
                     V_INSUFFICIENT, Citation, Document, Entry, Matter,
                     Verification)

# 简单路径枚举上限，防止极端图下耗时过长（实际事项规模远低于此）
MAX_PATHS = 2000


def snapshot_graph(session, matter_id):
    """读取某事项当前的引用图快照：在录条目、在册档号、有效引用边。"""
    entries = {
        e.id: e
        for e in session.query(Entry).filter(
            Entry.matter_id == matter_id, Entry.status == ENTRY_ACTIVE
        )
    }
    docs = {
        d.id: d
        for d in session.query(Document).filter(
            Document.matter_id == matter_id, Document.status == DOC_REGISTERED
        )
    }
    adj = {}
    if entries:
        q = session.query(Citation).filter(
            Citation.matter_id == matter_id,
            Citation.from_entry_id.in_(list(entries)),
        )
        for c in q:
            if c.to_entry_id is not None:
                if c.to_entry_id in entries:  # 被引条目须在录
                    adj.setdefault(c.from_entry_id, []).append(("entry", c.to_entry_id))
            elif c.to_document_id is not None:
                if c.to_document_id in docs:  # 被引档号须在册
                    adj.setdefault(c.from_entry_id, []).append(("document", c.to_document_id))
    return entries, docs, adj


def find_two_chains(start_entry_id, entries, docs, adj):
    """枚举起点到各在册原件的简单路径（条目可互引，故按访问集去环），
    寻找一对除起点外互不重合且档号不同的链。找到返回 (chain1, chain2)，否则 None。"""
    if start_entry_id not in entries:
        return None
    paths = []
    stack = [(start_entry_id, {start_entry_id}, [("entry", start_entry_id)])]
    while stack and len(paths) < MAX_PATHS:
        node, visited, path = stack.pop()
        for kind, tid in adj.get(node, ()):
            if kind == "document":
                paths.append(path + [("document", tid)])
                if len(paths) >= MAX_PATHS:
                    break
            elif tid not in visited:
                stack.append((tid, visited | {tid}, path + [("entry", tid)]))
    for i in range(len(paths)):
        p1 = paths[i]
        rest1 = set(p1[1:])  # 起点之外的节点
        doc1 = p1[-1][1]
        for j in range(i + 1, len(paths)):
            p2 = paths[j]
            doc2 = p2[-1][1]
            if docs[doc1].archive_no == docs[doc2].archive_no:
                continue  # 必须通向不同档号
            if rest1.isdisjoint(set(p2[1:])):
                return p1, p2
    return None


def chain_to_jsonable(path, entries, docs):
    out = []
    for kind, nid in path:
        if kind == "entry":
            e = entries[nid]
            out.append({"type": "entry", "id": e.id, "label": e.label})
        else:
            d = docs[nid]
            out.append({"type": "document", "id": d.id, "archive_no": d.archive_no})
    return out


def run_verification(verification_id, delay=0.0):
    """后台核查：先取快照计算，再条件写入结论。

    快照读取后即释放连接，核查（含模拟耗时的 delay）期间资料可正常改动；
    写入结论时仅当记录仍为“在途”才生效——若期间本事项资料变动，
    该核查已被置为“退回”，此处不再覆盖。
    """
    session = SessionLocal()
    try:
        ver = session.get(Verification, verification_id)
        if ver is None or ver.status != V_INFLIGHT:
            return
        matter_id = ver.matter_id
        start_id = ver.start_entry_id
        matter = session.get(Matter, matter_id)
        version0 = matter.version if matter else None
        entries, docs, adj = snapshot_graph(session, matter_id)
        result = find_two_chains(start_id, entries, docs, adj)
        if result:
            chain_a = json.dumps(chain_to_jsonable(result[0], entries, docs), ensure_ascii=False)
            chain_b = json.dumps(chain_to_jsonable(result[1], entries, docs), ensure_ascii=False)
            status = V_CONFIRMED
        else:
            chain_a = chain_b = None
            status = V_INSUFFICIENT
    finally:
        session.close()  # 释放快照，核查期间不占用数据库

    if delay and delay > 0:
        time.sleep(delay)  # 模拟核查耗时；此窗口内资料改动会使本核查被退回

    session = SessionLocal()
    try:
        session.execute(
            update(Verification)
            .where(Verification.id == verification_id, Verification.status == V_INFLIGHT)
            .values(
                status=status,
                chain_a=chain_a,
                chain_b=chain_b,
                matter_version=version0,
                updated_at=datetime.utcnow(),
            )
        )
        session.commit()
    except Exception:
        session.rollback()  # 写入失败：保留原状（仍为在途或已被退回）
    finally:
        session.close()
