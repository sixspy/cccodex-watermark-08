"""下游追索：只认同日接受留证，跨日新印不进追索结果。"""

from app.db import get_db, resolve_read_day
from app.domain import READ_DAY_MODE, STATUS_STAMPED, VALID_STAMP_SQL, day_relation
from app.services.errors import ServiceError


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        read_day, read_day_source = resolve_read_day(conn)

        sources = conn.execute(
            f"SELECT b.* FROM run_batches b WHERE b.watermark_value=? AND b.status=? AND "
            f"{VALID_STAMP_SQL} ORDER BY b.id",
            (value, STATUS_STAMPED),
        ).fetchall()
        if not sources:
            rejected = conn.execute(
                "SELECT b.run_code, b.biz_date, a.read_day, a.reason_code, a.reason "
                "FROM stamp_audit a JOIN run_batches b ON b.id=a.batch_id "
                "WHERE a.watermark_value=? AND a.result='rejected' "
                "ORDER BY a.id DESC LIMIT 1",
                (value,),
            ).fetchone()
            if rejected:
                detail = {
                    "run_code": rejected["run_code"],
                    "biz_date": rejected["biz_date"],
                    "audit_read_day": rejected["read_day"],
                    "reason_code": rejected["reason_code"],
                    "reason": rejected["reason"],
                    "read_day": read_day,
                    "read_day_source": read_day_source,
                    "read_day_mode": READ_DAY_MODE,
                }
                err = ServiceError(
                    f"该水印值命中的批次已被拒：{rejected['reason']}", 409
                )
                err.detail = detail
                raise err
            raise ServiceError("未找到该水印值的有效盖印批次", 404)

        codes = [s["run_code"] for s in sources]
        placeholders = ",".join("?" for _ in codes)
        edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders})",
            codes,
        ).fetchall()
        down_codes = sorted({e["downstream_run_code"] for e in edges})
        downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            rows = conn.execute(
                f"SELECT b.*, CASE WHEN {VALID_STAMP_SQL} THEN 1 ELSE 0 END AS has_valid_stamp "
                f"FROM run_batches b WHERE b.run_code IN ({ph2}) ORDER BY b.id",
                down_codes,
            ).fetchall()
            for r in rows:
                item = dict(r)
                item["relation"] = day_relation(item["biz_date"], read_day)
                item["stamp_valid"] = bool(item["has_valid_stamp"])
                item["stamp_note"] = (
                    "无同日接受留证"
                    if item["status"] == STATUS_STAMPED and not item["stamp_valid"]
                    else ""
                )
                downstream.append(item)

        source_items = []
        for s in sources:
            item = dict(s)
            item["relation"] = day_relation(item["biz_date"], read_day)
            item["stamp_valid"] = True
            source_items.append(item)

        return {
            "watermark_value": value,
            "read_day": read_day,
            "read_day_source": read_day_source,
            "read_day_mode": READ_DAY_MODE,
            "sources": source_items,
            "edges": [dict(e) for e in edges],
            "downstream": downstream,
        }
