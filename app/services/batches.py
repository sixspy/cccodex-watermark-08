"""批次盖印：读日闸门 + 写锁 + 留证。

判定只走 app.domain.evaluate_stamp_gate；读日只走 db.resolve_read_day。
盖印服务在提交瞬间（BEGIN IMMEDIATE 之后）读库内业务日再判，失败回短句，
并把拒因写进 stamp_audit 留证；跨日批次不改 run_batches。
"""

from app.db import get_db, immediate_tx, resolve_read_day
from app.domain import (
    READ_DAY_MODE,
    RELATION_CODES,
    REASON_ALREADY_STAMPED,
    REASON_EMPTY_VALUE,
    REASON_NOT_FOUND,
    STATUS_PENDING,
    STATUS_STAMPED,
    VALID_STAMP_SQL,
    day_relation,
    evaluate_stamp_gate,
)
from app.services.errors import ServiceError

_BATCH_WITH_GATE_SQL = f"""
SELECT b.*,
       CASE WHEN {VALID_STAMP_SQL} THEN 1 ELSE 0 END AS has_valid_stamp,
       CASE WHEN EXISTS (
         SELECT 1 FROM watermark_templates t
         WHERE t.table_name = b.table_name AND t.status='现行'
       ) THEN 1 ELSE 0 END AS has_template
FROM run_batches b
"""


def _decorate(row, read_day: str) -> dict:
    batch = dict(row)
    gate = evaluate_stamp_gate(
        biz_date=batch["biz_date"],
        status=batch["status"],
        read_day=read_day,
        has_template=bool(batch["has_template"]),
    )
    batch.update(
        {
            "relation": gate["relation"],
            "relation_code": RELATION_CODES[gate["relation"]],
            "stamp_allowed": gate["allowed"],
            "block_reason": gate["reason"],
            "block_reason_code": gate["reason_code"],
            "read_day": read_day,
            "read_day_source": None,
            "read_day_mode": READ_DAY_MODE,
        }
    )
    if batch["status"] == STATUS_STAMPED and not batch["has_valid_stamp"]:
        batch["stamp_valid"] = False
        batch["stamp_note"] = "无同日接受留证"
    else:
        batch["stamp_valid"] = bool(batch["has_valid_stamp"])
        batch["stamp_note"] = ""
    return batch


def _read_day_in(conn) -> tuple[str, str]:
    return resolve_read_day(conn)


def list_batches(
    db_path: str | None = None,
    status: str | None = None,
    scope: str | None = None,
) -> dict:
    with get_db(db_path) as conn:
        read_day, read_day_source = _read_day_in(conn)
        sql = _BATCH_WITH_GATE_SQL
        params: list = []
        if status:
            sql += " WHERE b.status=?"
            params.append(status)
        sql += " ORDER BY b.biz_date DESC, b.id"
        rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = _decorate(row, read_day)
            item["read_day_source"] = read_day_source
            if scope == "cross_day" and item["relation"] != "跨日":
                continue
            if scope == "stampable" and not item["stamp_allowed"]:
                continue
            items.append(item)
        return {
            "read_day": read_day,
            "read_day_source": read_day_source,
            "read_day_mode": READ_DAY_MODE,
            "items": items,
        }


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        read_day, read_day_source = _read_day_in(conn)
        row = conn.execute(
            _BATCH_WITH_GATE_SQL + " WHERE b.id=?", (batch_id,)
        ).fetchone()
        if not row:
            return None
        batch = _decorate(row, read_day)
        batch["read_day_source"] = read_day_source
        batch["audits"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM stamp_audit WHERE batch_id=? ORDER BY id DESC", (batch_id,)
            ).fetchall()
        ]
        return batch


def cross_day_batches(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        read_day, read_day_source = _read_day_in(conn)
        rows = conn.execute(
            _BATCH_WITH_GATE_SQL + " WHERE b.biz_date < ? ORDER BY b.biz_date, b.id",
            (read_day,),
        ).fetchall()
        items = []
        for row in rows:
            item = _decorate(row, read_day)
            item["read_day_source"] = read_day_source
            items.append(item)
        future = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date > ?", (read_day,)
        ).fetchone()["c"]
        return {
            "read_day": read_day,
            "read_day_source": read_day_source,
            "items": items,
            "count": len(items),
            "future_count": future,
        }


def waterline(db_path: str | None = None) -> dict:
    result = list_batches(db_path)
    read_day = result["read_day"]
    buckets = {"当日": [], "跨日": [], "未来日": []}
    for item in result["items"]:
        buckets[item["relation"]].append(item)
    return {
        "read_day": read_day,
        "read_day_source": result["read_day_source"],
        "read_day_mode": READ_DAY_MODE,
        "buckets": buckets,
        "counts": {
            "当日": len(buckets["当日"]),
            "跨日": len(buckets["跨日"]),
            "未来日": len(buckets["未来日"]),
        },
        "stampable_count": sum(1 for i in result["items"] if i["stamp_allowed"]),
        "invalid_stamp_count": sum(
            1
            for i in result["items"]
            if i["status"] == STATUS_STAMPED and not i["stamp_valid"]
        ),
    }


def _record_audit(
    conn,
    batch,
    read_day: str,
    read_day_source: str,
    watermark_value: str,
    result: str,
    reason_code: str,
    reason: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO stamp_audit(batch_id, run_code, table_name, biz_date, read_day, "
        "read_day_source, read_day_mode, watermark_value, result, reason_code, reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch["id"],
            batch["run_code"],
            batch["table_name"],
            batch["biz_date"],
            read_day,
            read_day_source,
            READ_DAY_MODE,
            watermark_value or None,
            result,
            reason_code,
            reason,
        ),
    )
    return int(cur.lastrowid)


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    rejection = None
    audit_id = None

    with immediate_tx(db_path) as conn:
        # 提交瞬间读库内业务日，再判定 —— 打开页面时的快照一律不用。
        read_day, read_day_source = resolve_read_day(conn)
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError(REASON_NOT_FOUND, 404)
        has_template = (
            conn.execute(
                "SELECT 1 FROM watermark_templates WHERE table_name=? AND status='现行'",
                (batch["table_name"],),
            ).fetchone()
            is not None
        )
        gate = evaluate_stamp_gate(
            biz_date=batch["biz_date"],
            status=batch["status"],
            read_day=read_day,
            has_template=has_template,
        )
        if gate["allowed"] and not value:
            gate = {
                "allowed": False,
                "relation": gate["relation"],
                "reason_code": "empty_value",
                "reason": REASON_EMPTY_VALUE,
            }

        if not gate["allowed"]:
            audit_id = _record_audit(
                conn,
                batch,
                read_day,
                read_day_source,
                value,
                "rejected",
                gate["reason_code"],
                gate["reason"],
            )
            status_code = 400 if gate["reason_code"] == "empty_value" else 409
            rejection = ServiceError(gate["reason"], status_code)
            rejection.detail = {
                "batch_id": batch_id,
                "run_code": batch["run_code"],
                "biz_date": batch["biz_date"],
                "relation": gate["relation"],
                "reason_code": gate["reason_code"],
                "read_day": read_day,
                "read_day_source": read_day_source,
                "read_day_mode": READ_DAY_MODE,
                "audit_id": audit_id,
                "watermark_written": False,
            }
        else:
            cur = conn.execute(
                "UPDATE run_batches SET status=?, watermark_value=?, "
                "stamped_at=datetime('now') WHERE id=? AND status=?",
                (STATUS_STAMPED, value, batch_id, STATUS_PENDING),
            )
            if cur.rowcount != 1:
                audit_id = _record_audit(
                    conn,
                    batch,
                    read_day,
                    read_day_source,
                    value,
                    "rejected",
                    "already_stamped",
                    REASON_ALREADY_STAMPED,
                )
                rejection = ServiceError(REASON_ALREADY_STAMPED, 409)
                rejection.detail = {
                    "batch_id": batch_id,
                    "reason_code": "already_stamped",
                    "read_day": read_day,
                    "read_day_source": read_day_source,
                    "read_day_mode": READ_DAY_MODE,
                    "audit_id": audit_id,
                    "watermark_written": False,
                }
            else:
                audit_id = _record_audit(
                    conn,
                    batch,
                    read_day,
                    read_day_source,
                    value,
                    "accepted",
                    "",
                    "",
                )

    if rejection is not None:
        raise rejection

    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        batch = dict(row)
        batch.update(
            {
                "read_day": read_day,
                "read_day_source": read_day_source,
                "read_day_mode": READ_DAY_MODE,
                "read_day_relation": day_relation(batch["biz_date"], read_day),
                "stamp_valid": True,
                "audit_id": audit_id,
            }
        )
        return batch
