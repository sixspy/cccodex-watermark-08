"""缺口对账：以读日为默认对账日，只认同日接受留证的有效盖印。"""

from app.db import get_db, resolve_read_day
from app.domain import (
    READ_DAY_MODE,
    STATUS_STAMPED,
    STATUS_VOID,
    VALID_STAMP_SQL,
    parse_day,
)
from app.services.errors import ServiceError


def list_reports(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        read_day, read_day_source = resolve_read_day(conn)
        rows = conn.execute("SELECT * FROM gap_reports ORDER BY id DESC").fetchall()
    return {
        "read_day": read_day,
        "read_day_source": read_day_source,
        "read_day_mode": READ_DAY_MODE,
        "items": [dict(r) for r in rows],
    }


def run_gap_report(as_of_date: str | None = None, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        read_day, read_day_source = resolve_read_day(conn)
        requested = (as_of_date or "").strip()
        if not requested:
            day = read_day
            day_source = read_day_source
        else:
            try:
                day = parse_day(requested)
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            day_source = "request.as_of_date"

        expected = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches b WHERE b.biz_date<=? AND b.status!=?",
            (day, STATUS_VOID),
        ).fetchone()["c"]
        stamped = conn.execute(
            f"SELECT COUNT(*) AS c FROM run_batches b WHERE b.biz_date<=? AND b.status=? AND "
            f"{VALID_STAMP_SQL}",
            (day, STATUS_STAMPED),
        ).fetchone()["c"]
        invalid = conn.execute(
            f"SELECT COUNT(*) AS c FROM run_batches b WHERE b.biz_date<=? AND b.status=? AND NOT "
            f"{VALID_STAMP_SQL}",
            (day, STATUS_STAMPED),
        ).fetchone()["c"]
        cross_day = conn.execute(
            "SELECT COUNT(*) AS c FROM run_batches b WHERE b.biz_date < ?", (read_day,)
        ).fetchone()["c"]
        gap = expected - stamped
        summary = (
            f"截至 {day}：应盖印 {expected}，有效盖印 {stamped}，缺口 {gap}"
        )
        if invalid:
            summary += f"；伪盖印 {invalid}"
        cur = conn.execute(
            "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, gap_count, "
            "summary, read_day, read_day_source, read_day_mode, invalid_stamp_count, "
            "cross_day_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                day,
                expected,
                stamped,
                gap,
                summary,
                read_day,
                day_source,
                READ_DAY_MODE,
                invalid,
                cross_day,
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
