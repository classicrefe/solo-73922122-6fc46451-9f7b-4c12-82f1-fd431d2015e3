"""关键操作的自测：归档、登记/撤回、改录、双链核查、佐证不足、
待复核、在途退回、跨事项隔离、写入失败回滚。"""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.database import Base, SessionLocal, engine
from app.main import app, get_session


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.state.verify_delay = 0.0
    yield
    app.dependency_overrides.clear()
    app.state.verify_delay = 0.0


@pytest.fixture
def client():
    return TestClient(app)


# ---------- 辅助 ----------

def make_matter(client, old="中山路", new="解放路", date="2026-01-01"):
    r = client.post("/matters", json={"old_name": old, "new_name": new, "effective_date": date})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_doc(client, mid, no):
    r = client.post(f"/matters/{mid}/documents", json={"archive_no": no})
    assert r.status_code == 201, r.text
    return r.json()


def add_entry(client, mid, label, citations=None):
    r = client.post(f"/matters/{mid}/entries",
                    json={"label": label, "citations": citations or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def verify(client, mid, entry_id, wait=True):
    r = client.post(f"/matters/{mid}/verifications?wait={str(wait).lower()}",
                    json={"entry_id": entry_id})
    assert r.status_code == 202, r.text
    return r.json()


def wait_done(client, vid, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = client.get(f"/verifications/{vid}").json()
        if v["status"] != "在途":
            return v
        time.sleep(0.05)
    raise AssertionError("核查未在限定时间内结束")


def build_two_chain_matter(client, old="中山路", new="解放路", date="2026-01-01"):
    """E1（市志）→ E2（县志卷三）→ 档A-001；E1 → E3（地名志）→ 档A-002。"""
    mid = make_matter(client, old, new, date)
    add_doc(client, mid, "A-001")
    add_doc(client, mid, "A-002")
    e2 = add_entry(client, mid, "县志卷三", [{"archive_no": "A-001"}])
    e3 = add_entry(client, mid, "地名志", [{"archive_no": "A-002"}])
    e1 = add_entry(client, mid, "市志", [{"entry_id": e2}, {"entry_id": e3}])
    return mid, e1, e2, e3


# ---------- 事项归档 ----------

def test_matter_create_and_duplicate(client):
    mid = make_matter(client)
    matters = client.get("/matters").json()
    assert any(m["id"] == mid and m["old_name"] == "中山路"
               and m["new_name"] == "解放路" and m["effective_date"] == "2026-01-01"
               for m in matters)
    r = client.post("/matters", json={"old_name": "中山路", "new_name": "解放路",
                                      "effective_date": "2026-01-01"})
    assert r.status_code == 409
    # 任一要素不同即属另一事项
    r = client.post("/matters", json={"old_name": "中山路", "new_name": "解放路",
                                      "effective_date": "2026-02-01"})
    assert r.status_code == 201
    assert client.get("/matters/99999").status_code == 404


# ---------- 档号登记与撤回 ----------

def test_document_register_withdraw_reregister(client):
    mid = make_matter(client)
    doc = add_doc(client, mid, "A-001")
    assert doc["status"] == "在册"
    assert client.post(f"/matters/{mid}/documents", json={"archive_no": "A-001"}).status_code == 409
    r = client.delete(f"/matters/{mid}/documents/A-001")
    assert r.status_code == 200 and r.json()["status"] == "已撤回"
    assert client.delete(f"/matters/{mid}/documents/A-001").status_code == 404
    r = client.post(f"/matters/{mid}/documents", json={"archive_no": "A-001"})
    assert r.status_code == 201 and r.json()["status"] == "在册"


# ---------- 引用校验：限于同一事项 ----------

def test_citation_limited_to_same_matter(client):
    mid1 = make_matter(client, old="中山路", new="解放路")
    mid2 = make_matter(client, old="和平街", new="建设街")
    add_doc(client, mid1, "A-001")
    e_other = add_entry(client, mid2, "外事项条目")
    # 引用其他事项的条目 → 422，且条目整体不落入库（事务回滚）
    r = client.post(f"/matters/{mid1}/entries",
                    json={"label": "坏条目", "citations": [{"entry_id": e_other}]})
    assert r.status_code == 422
    detail = client.get(f"/matters/{mid1}").json()
    assert all(e["label"] != "坏条目" for e in detail["entries"])
    # 引用其他事项的档号 → 422
    e1 = add_entry(client, mid1, "市志")
    r = client.put(f"/matters/{mid1}/entries/{e1}/citations",
                   json={"citations": [{"archive_no": "B-999"}]})
    assert r.status_code == 422
    # 自引用 → 422；目标缺失 → 422
    assert client.put(f"/matters/{mid1}/entries/{e1}/citations",
                      json={"citations": [{"entry_id": e1}]}).status_code == 422
    assert client.put(f"/matters/{mid1}/entries/{e1}/citations",
                      json={"citations": [{}]}).status_code == 422
    # 引用已撤回档号 → 422
    client.delete(f"/matters/{mid1}/documents/A-001")
    assert client.put(f"/matters/{mid1}/entries/{e1}/citations",
                      json={"citations": [{"archive_no": "A-001"}]}).status_code == 422


# ---------- 双链核查 ----------

def test_two_chain_confirmed(client):
    mid, e1, e2, e3 = build_two_chain_matter(client)
    v = verify(client, mid, e1)
    assert v["status"] == "佐证成立"
    for chain in (v["chain_a"], v["chain_b"]):
        assert chain[0]["type"] == "entry" and chain[0]["id"] == e1
        assert chain[-1]["type"] == "document"
    archives = {v["chain_a"][-1]["archive_no"], v["chain_b"][-1]["archive_no"]}
    assert archives == {"A-001", "A-002"}  # 通向不同档号
    nodes_a = {n["id"] for n in v["chain_a"][1:]}
    nodes_b = {n["id"] for n in v["chain_b"][1:]}
    assert nodes_a.isdisjoint(nodes_b)  # 除起点外互不重合
    # 结论已保存，可按条目查询
    lst = client.get(f"/matters/{mid}/verifications", params={"entry_id": e1}).json()
    assert any(x["id"] == v["id"] and x["status"] == "佐证成立" for x in lst)


def test_insufficient_when_same_document(client):
    # 两个出处最终都转引自同一份原件 → 佐证不足
    mid = make_matter(client)
    add_doc(client, mid, "A-001")
    e2 = add_entry(client, mid, "县志", [{"archive_no": "A-001"}])
    e3 = add_entry(client, mid, "地名志", [{"archive_no": "A-001"}])
    e1 = add_entry(client, mid, "市志", [{"entry_id": e2}, {"entry_id": e3}])
    v = verify(client, mid, e1)
    assert v["status"] == "佐证不足"
    assert v["chain_a"] is None and v["chain_b"] is None


def test_insufficient_when_chains_overlap(client):
    # 两条链在 E2 处重合 → 佐证不足
    mid = make_matter(client)
    add_doc(client, mid, "A-001")
    add_doc(client, mid, "A-002")
    e2 = add_entry(client, mid, "县志", [{"archive_no": "A-001"}, {"archive_no": "A-002"}])
    e1 = add_entry(client, mid, "市志", [{"entry_id": e2}])
    v = verify(client, mid, e1)
    assert v["status"] == "佐证不足"


def test_mutual_citation_cycle_ok(client):
    # 条目互引成环，仍应正常判定且不死循环
    mid = make_matter(client)
    add_doc(client, mid, "A-001")
    add_doc(client, mid, "A-002")
    e2 = add_entry(client, mid, "县志")
    e3 = add_entry(client, mid, "地名志")
    client.put(f"/matters/{mid}/entries/{e2}/citations",
               json={"citations": [{"entry_id": e3}, {"archive_no": "A-001"}]})
    client.put(f"/matters/{mid}/entries/{e3}/citations",
               json={"citations": [{"entry_id": e2}, {"archive_no": "A-002"}]})
    e1 = add_entry(client, mid, "市志", [{"entry_id": e2}, {"entry_id": e3}])
    v = verify(client, mid, e1)
    assert v["status"] == "佐证成立"


def test_direct_document_citations(client):
    # 起点条目直接注明两个不同档号 → 佐证成立
    mid = make_matter(client)
    add_doc(client, mid, "A-001")
    add_doc(client, mid, "A-002")
    e1 = add_entry(client, mid, "市志", [{"archive_no": "A-001"}, {"archive_no": "A-002"}])
    v = verify(client, mid, e1)
    assert v["status"] == "佐证成立"
    assert len(v["chain_a"]) == 2 and len(v["chain_b"]) == 2


# ---------- 资料变动：旧结论待复核、其他事项不受影响 ----------

def test_stale_on_change_and_other_matter_unaffected(client):
    mid_a, e1a, _, _ = build_two_chain_matter(client, old="中山路", new="解放路")
    mid_b, e1b, _, _ = build_two_chain_matter(client, old="和平街", new="建设街")
    va = verify(client, mid_a, e1a)
    vb = verify(client, mid_b, e1b)
    assert va["status"] == vb["status"] == "佐证成立"
    # 本事项资料新增 → 旧结论待复核
    add_doc(client, mid_a, "A-003")
    assert client.get(f"/verifications/{va['id']}").json()["status"] == "待复核"
    # 其他事项的结论不受影响
    assert client.get(f"/verifications/{vb['id']}").json()["status"] == "佐证成立"
    # 重新核查可再次得出成立
    va2 = verify(client, mid_a, e1a)
    assert va2["status"] == "佐证成立"


def test_inflight_verification_returned_on_change(client):
    app.state.verify_delay = 1.0  # 拉长核查窗口
    mid, e1, _, _ = build_two_chain_matter(client)
    r = client.post(f"/matters/{mid}/verifications", json={"entry_id": e1})
    assert r.status_code == 202 and r.json()["status"] == "在途"
    vid = r.json()["id"]
    time.sleep(0.1)
    add_doc(client, mid, "A-003")  # 核查期间改动本事项资料
    v = wait_done(client, vid)
    assert v["status"] == "退回"
    assert v["chain_a"] is None and v["chain_b"] is None


def test_change_in_other_matter_does_not_return_inflight(client):
    app.state.verify_delay = 0.8
    mid_a, e1a, _, _ = build_two_chain_matter(client, old="中山路", new="解放路")
    mid_b = make_matter(client, old="和平街", new="建设街")
    r = client.post(f"/matters/{mid_a}/verifications", json={"entry_id": e1a})
    vid = r.json()["id"]
    time.sleep(0.1)
    add_doc(client, mid_b, "B-001")  # 只动其他事项
    v = wait_done(client, vid)
    assert v["status"] == "佐证成立"


# ---------- 改录引用 ----------

def test_update_citations_changes_conclusion(client):
    mid, e1, e2, e3 = build_two_chain_matter(client)
    v1 = verify(client, mid, e1)
    assert v1["status"] == "佐证成立"
    # 改录：E3 也指向 A-001 → 两条链同源，佐证不足；旧结论待复核
    r = client.put(f"/matters/{mid}/entries/{e3}/citations",
                   json={"citations": [{"archive_no": "A-001"}]})
    assert r.status_code == 200
    assert client.get(f"/verifications/{v1['id']}").json()["status"] == "待复核"
    v2 = verify(client, mid, e1)
    assert v2["status"] == "佐证不足"
    # 改回 A-002 → 再次成立
    client.put(f"/matters/{mid}/entries/{e3}/citations",
               json={"citations": [{"archive_no": "A-002"}]})
    v3 = verify(client, mid, e1)
    assert v3["status"] == "佐证成立"


# ---------- 撤回条目/档号影响核查 ----------

def test_withdraw_entry_breaks_chain(client):
    mid, e1, e2, e3 = build_two_chain_matter(client)
    client.delete(f"/matters/{mid}/entries/{e3}")
    v = verify(client, mid, e1)
    assert v["status"] == "佐证不足"


def test_withdraw_document_breaks_chain(client):
    mid, e1, e2, e3 = build_two_chain_matter(client)
    client.delete(f"/matters/{mid}/documents/A-002")
    v = verify(client, mid, e1)
    assert v["status"] == "佐证不足"


def test_verify_with_invalid_start_entry(client):
    mid, e1, e2, e3 = build_two_chain_matter(client)
    assert client.post(f"/matters/{mid}/verifications",
                       json={"entry_id": 99999}).status_code == 404
    client.delete(f"/matters/{mid}/entries/{e3}")
    assert client.post(f"/matters/{mid}/verifications",
                       json={"entry_id": e3}).status_code == 404


# ---------- 写入失败：沿用原资料和结论 ----------

def test_write_failure_keeps_data_and_conclusion(client):
    mid, e1, _, _ = build_two_chain_matter(client)
    v = verify(client, mid, e1)
    assert v["status"] == "佐证成立"
    version_before = client.get(f"/matters/{mid}").json()["matter"]["version"]

    def broken_session():
        s = SessionLocal()
        def boom():
            raise SQLAlchemyError("simulated write failure")
        s.commit = boom
        try:
            yield s
        finally:
            s.rollback()
            s.close()

    app.dependency_overrides[get_session] = broken_session
    try:
        r = client.post(f"/matters/{mid}/documents", json={"archive_no": "A-009"})
        assert r.status_code == 500
        assert "沿用原资料" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()

    detail = client.get(f"/matters/{mid}").json()
    assert all(d["archive_no"] != "A-009" for d in detail["documents"])  # 资料未变
    assert detail["matter"]["version"] == version_before                 # 版本未变
    assert client.get(f"/verifications/{v['id']}").json()["status"] == "佐证成立"  # 结论未变


# ---------- 详情与列表 ----------

def test_matter_detail_contains_all(client):
    mid, e1, e2, e3 = build_two_chain_matter(client)
    verify(client, mid, e1)
    detail = client.get(f"/matters/{mid}").json()
    assert detail["matter"]["id"] == mid
    assert {d["archive_no"] for d in detail["documents"]} == {"A-001", "A-002"}
    labels = {e["label"] for e in detail["entries"]}
    assert labels == {"市志", "县志卷三", "地名志"}
    e1_out = next(e for e in detail["entries"] if e["id"] == e1)
    assert len(e1_out["citations"]) == 2
    assert len(detail["verifications"]) == 1
    assert detail["verifications"][0]["status"] == "佐证成立"
