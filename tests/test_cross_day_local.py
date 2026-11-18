"""跨日水线回归测试：与四处视图同源、留证、追索、对账、并发。

对应题面四条要求：
1. 跨日运行码不得混进当日盖印池；
2. 读日在提交瞬间取库内业务日（非打开页面快照）；
3. 业务日 / 门槛同源，跨日仍写入即失败；
4. 并发抢同一批只能有一次新水印，且追索复跑不得吃到跨日新印。
"""

import sqlite3
import threading

import pytest

from app import domain
from app.db import get_db, init_db, set_read_day
from app.services import (
    cross_day_batches,
    get_batch,
    list_batches,
    run_gap_report,
    stamp_batch,
    trace_downstream,
    waterline,
)
from app.services.errors import ServiceError


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "unit.db")
    init_db(path)
    return path


def _batch_id(db, run_code):
    with get_db(db) as conn:
        row = conn.execute(
            "SELECT id FROM run_batches WHERE run_code=?", (run_code,)
        ).fetchone()
    return row["id"]


def _force_write_cross_day_stamp(db, bid, value):
    """绕过服务层直接写库，模拟“并发灌数”把跨日新印塞进去。"""
    with get_db(db) as conn:
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=?",
            (value, bid),
        )


def test_cross_day_list_does_not_mix_with_current_day_pool(db):
    day, source = set_read_day("2026-11-14", db)
    assert (day, source) == ("2026-11-14", domain.READ_DAY_SOURCE)

    pool = list_batches(db, scope="stampable")["items"]
    assert pool, "当日池不为空"
    assert all(b["biz_date"] == "2026-11-14" for b in pool)
    assert not any(b["run_code"] == "pay-20261113-01" for b in pool)

    cross = cross_day_batches(db)
    codes = {b["run_code"] for b in cross["items"]}
    assert "pay-20261113-01" in codes
    assert all(b["relation"] == domain.DAY_CROSS for b in cross["items"])
    assert all(not b["stamp_allowed"] for b in cross["items"])


def test_list_batches_can_scope_to_cross_day(db):
    set_read_day("2026-11-14", db)
    result = list_batches(db, scope="cross_day")
    codes = {b["run_code"] for b in result["items"]}
    assert codes == {"ord-20261113-01", "pay-20261113-01"}


def test_read_day_source_shared_across_four_surfaces(db):
    set_read_day("2026-11-14", db)
    line = waterline(db)
    detail = get_batch(_batch_id(db, "pay-20261113-01"), db)
    cross = cross_day_batches(db)
    trace = trace_downstream("WM-ORD-131", db)

    days = {
        line["read_day"],
        detail["read_day"],
        cross["read_day"],
        trace["read_day"],
    }
    sources = {
        line["read_day_source"],
        detail["read_day_source"],
        cross["read_day_source"],
        trace["read_day_source"],
    }
    assert days == {"2026-11-14"}
    assert sources == {domain.READ_DAY_SOURCE}


def test_read_day_falls_back_to_max_biz_date(tmp_path):
    path = str(tmp_path / "noday.db")
    init_db(path)
    with get_db(path) as conn:
        conn.execute("DELETE FROM meta WHERE key=?", (domain.READ_DAY_META_KEY,))

    info = list_batches(path)
    assert info["read_day"] == "2026-11-14"
    assert info["read_day_source"] == domain.READ_DAY_FALLBACK_SOURCE


def test_cross_day_stamp_rejected_and_audited(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "pay-20261113-01")

    with pytest.raises(ServiceError) as err:
        stamp_batch(bid, "WM-CROSS-1", db)
    assert err.value.message == domain.REASON_CROSS_DAY
    assert err.value.detail["relation"] == domain.DAY_CROSS
    assert err.value.detail["watermark_written"] is False

    with get_db(db) as conn:
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (bid,)).fetchone()
        audits = conn.execute(
            "SELECT * FROM stamp_audit WHERE batch_id=?", (bid,)
        ).fetchall()
    assert row["status"] == "待盖印"
    assert row["watermark_value"] is None
    assert len(audits) == 1
    assert audits[0]["result"] == "rejected"
    assert audits[0]["reason_code"] == "cross_day"
    assert audits[0]["read_day"] == "2026-11-14"
    assert audits[0]["biz_date"] == "2026-11-13"


def test_gate_uses_read_day_at_submit_not_page_open(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "ord-20261114-01")
    opened = get_batch(bid, db)
    assert opened["stamp_allowed"] is True

    # 页面打开后又跨了日：快照说可盖，提交时必须按库内读日重新判。
    set_read_day("2026-11-15", db)
    with pytest.raises(ServiceError) as err:
        stamp_batch(bid, "WM-CRIT-1", db)
    assert err.value.message == domain.REASON_CROSS_DAY
    assert err.value.detail["relation"] == domain.DAY_CROSS
    assert err.value.detail["read_day"] == "2026-11-15"


def test_same_day_stamp_writes_audit_and_valid_flag(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "ord-20261114-01")
    batch = stamp_batch(bid, "WM-TODAY-1", db)
    assert batch["status"] == "已盖印"
    assert batch["stamp_valid"] is True

    detail = get_batch(bid, db)
    accepted = [a for a in detail["audits"] if a["result"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["biz_date"] == accepted[0]["read_day"] == "2026-11-14"


def test_reject_then_same_day_accept_keeps_both_records(db):
    bid = _batch_id(db, "pay-20261113-01")
    set_read_day("2026-11-14", db)
    with pytest.raises(ServiceError):
        stamp_batch(bid, "WM-CROSS-2", db)

    set_read_day("2026-11-13", db)
    batch = stamp_batch(bid, "WM-CROSS-2", db)
    assert batch["status"] == "已盖印"

    audits = get_batch(bid, db)["audits"]
    assert {a["result"] for a in audits} == {"rejected", "accepted"}


def test_legacy_db_upgrade_backfills_audit_once(tmp_path):
    path = str(tmp_path / "legacy.db")
    init_db(path)
    with get_db(path) as conn:
        conn.execute("DELETE FROM stamp_audit")
        conn.execute("DELETE FROM meta WHERE key='stamp_audit_backfilled'")

    init_db(path)
    with get_db(path) as conn:
        accepted = conn.execute(
            "SELECT * FROM stamp_audit WHERE result='accepted'"
        ).fetchall()
        bad = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_audit WHERE biz_date != read_day"
        ).fetchone()["c"]
    assert len(accepted) == 1
    assert accepted[0]["run_code"] == "ord-20261113-01"
    assert bad == 0

    # 回填只做一次：之后伪造的历史盖印不得补出“有效留证”。
    with get_db(path) as conn:
        conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value='WM-LEGACY-FAKE' "
            "WHERE run_code='pay-20261113-01'"
        )
    init_db(path)
    init_db(path)
    with get_db(path) as conn:
        forged = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_audit WHERE watermark_value='WM-LEGACY-FAKE'"
        ).fetchone()["c"]
    assert forged == 0


def test_trace_ignores_cross_day_new_stamp(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "pay-20261114-01")
    _force_write_cross_day_stamp(db, bid, "WM-FORGED-1")

    with pytest.raises(ServiceError) as err:
        trace_downstream("WM-FORGED-1", db)
    assert "未找到该水印值的有效盖印批次" in err.value.message


def test_trace_flags_rejected_watermark(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "pay-20261113-01")
    with pytest.raises(ServiceError):
        stamp_batch(bid, "WM-CROSS-3", db)

    with pytest.raises(ServiceError) as err:
        trace_downstream("WM-CROSS-3", db)
    assert err.value.status_code == 409
    assert err.value.detail["reason_code"] == "cross_day"
    assert err.value.detail["read_day"] == "2026-11-14"


def test_trace_rerun_never_returns_cross_day_stamp(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "pay-20261114-01")
    _force_write_cross_day_stamp(db, bid, "WM-REPEAT-1")
    for _ in range(3):
        with pytest.raises(ServiceError):
            trace_downstream("WM-REPEAT-1", db)


def test_gap_counts_only_valid_stamps(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "pay-20261114-01")
    _force_write_cross_day_stamp(db, bid, "WM-FORGED-GAP")

    report = run_gap_report("2026-11-14", db)
    assert report["invalid_stamp_count"] >= 1
    assert report["gap_count"] >= 1
    assert report["read_day"] == "2026-11-14"


def test_concurrent_stamp_double_write_is_blocked(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "ord-20261114-01")
    results = []
    lock = threading.Lock()

    def worker(idx):
        try:
            stamp_batch(bid, "WM-RACE-" + str(idx), db)
            with lock:
                results.append(("ok", idx))
        except ServiceError as exc:
            with lock:
                results.append(("err", exc.message))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1, results
    assert all("已盖印" in e[1] or "留证" in e[1] for e in errs), results

    with get_db(db) as conn:
        accepted = conn.execute(
            "SELECT COUNT(*) AS c FROM stamp_audit WHERE batch_id=? AND result='accepted'",
            (bid,),
        ).fetchone()["c"]
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (bid,)).fetchone()
    assert accepted == 1
    assert row["watermark_value"] == "WM-RACE-" + str(oks[0][1])


def test_unique_index_blocks_second_accepted_evidence(db):
    set_read_day("2026-11-14", db)
    bid = _batch_id(db, "ord-20261114-01")
    stamp_batch(bid, "WM-UNIQ-1", db)

    with pytest.raises(sqlite3.IntegrityError):
        with get_db(db) as conn:
            conn.execute(
                "INSERT INTO stamp_audit(batch_id, run_code, table_name, biz_date, "
                "read_day, read_day_source, read_day_mode, watermark_value, result) "
                "VALUES (?,?,?,?,?,?,?,?,'accepted')",
                (
                    bid,
                    "ord-20261114-01",
                    "dw_orders_d",
                    "2026-11-14",
                    "2026-11-14",
                    domain.READ_DAY_SOURCE,
                    domain.READ_DAY_MODE,
                    "WM-UNIQ-2",
                ),
            )


# ---------------- HTTP 面：四处同源、拒因可见 ----------------


@pytest.mark.asyncio
async def test_http_cross_day_detail_and_stamp_reason(authed):
    batches = (await (await authed.get("/api/batches")).json())["items"]
    cross = [b for b in batches if b["relation"] == "跨日"]
    assert cross
    target = next(b for b in cross if b["status"] == "待盖印")

    detail = (await (await authed.get(f"/api/batches/{target['id']}")).json())["batch"]
    assert detail["stamp_allowed"] is False
    assert detail["block_reason"] == domain.REASON_CROSS_DAY
    assert detail["read_day_source"]

    r = await authed.post(
        f"/api/batches/{target['id']}/stamp", json={"watermark_value": "WM-X"}
    )
    assert r.status == 409
    body = await r.json()
    assert body["error"] == domain.REASON_CROSS_DAY
    assert body["detail"]["reason_code"] == "cross_day"
    assert body["detail"]["watermark_written"] is False


@pytest.mark.asyncio
async def test_http_read_day_bad_format(authed):
    r = await authed.post("/api/read-day", json={"read_day": "2026/11/14"})
    assert r.status == 400
    assert domain.REASON_BAD_DAY in (await r.json())["error"]


@pytest.mark.asyncio
async def test_http_waterline_matches_read_day(authed):
    line = await (await authed.get("/api/waterline")).json()
    cross = await (await authed.get("/api/batches/cross-day")).json()
    detail = (
        await (await authed.get(f"/api/batches/{cross['items'][0]['id']}")).json()
    )
    assert (
        line["read_day"]
        == cross["read_day"]
        == detail["batch"]["read_day"]
    )
    assert (
        line["read_day_source"]
        == cross["read_day_source"]
        == detail["batch"]["read_day_source"]
    )


@pytest.mark.asyncio
async def test_http_gap_defaults_to_read_day(authed):
    r = await authed.post("/api/gap-reports", json={})
    assert r.status == 200
    report = (await r.json())["report"]
    assert report["read_day"]
    assert report["as_of_date"] == report["read_day"]
