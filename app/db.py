import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.config import Config
from app.domain import (
    READ_DAY_DEFAULT,
    READ_DAY_FALLBACK_SOURCE,
    READ_DAY_META_KEY,
    READ_DAY_MODE,
    READ_DAY_SOURCE,
    parse_day,
)

BUSY_TIMEOUT_MS = 5000


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_tx(db_path: str | None = None, attempts: int = 4):
    """写锁事务：BEGIN IMMEDIATE + 忙等重试，杜绝并发双写。"""
    from app.services.errors import ServiceError

    last_error = None
    for attempt in range(attempts):
        conn = connect(db_path)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            conn.close()
            last_error = exc
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                time.sleep(0.05 * (2**attempt))
                continue
            raise
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return
    raise ServiceError("写入繁忙，请重试", 503) from last_error


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stamp_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  run_code TEXT NOT NULL,
  table_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  read_day TEXT NOT NULL,
  read_day_source TEXT NOT NULL,
  read_day_mode TEXT NOT NULL,
  watermark_value TEXT,
  result TEXT NOT NULL CHECK (result IN ('accepted','rejected')),
  reason_code TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 每批最多一条接受留证：并发灌数即便绕过服务层，也无法双写新水印。
CREATE UNIQUE INDEX IF NOT EXISTS ux_stamp_audit_accepted
  ON stamp_audit(batch_id) WHERE result = 'accepted';

CREATE INDEX IF NOT EXISTS ix_stamp_audit_result
  ON stamp_audit(result, biz_date, read_day);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

GAP_REPORT_EXTRA_COLUMNS = {
    "read_day": "TEXT",
    "read_day_source": "TEXT",
    "read_day_mode": "TEXT",
    "invalid_stamp_count": "INTEGER NOT NULL DEFAULT 0",
    "cross_day_count": "INTEGER NOT NULL DEFAULT 0",
}

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
    ("dw_payments_d", "pay-20261113-01", "2026-11-13", "待盖印", None),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
    ("pay-20261113-01", "gmv-20261114-01"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(gap_reports)").fetchall()
    }
    for name, definition in GAP_REPORT_EXTRA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE gap_reports ADD COLUMN {name} {definition}")

    marker = conn.execute(
        "SELECT value FROM meta WHERE key='stamp_audit_backfilled'"
    ).fetchone()
    if marker:
        return
    legacy = conn.execute(
        "SELECT b.id, b.run_code, b.table_name, b.biz_date, b.watermark_value, b.stamped_at "
        "FROM run_batches b WHERE b.status='已盖印' AND NOT EXISTS ("
        "SELECT 1 FROM stamp_audit a WHERE a.batch_id=b.id AND a.result='accepted')"
    ).fetchall()
    for row in legacy:
        conn.execute(
            "INSERT INTO stamp_audit(batch_id, run_code, table_name, biz_date, read_day, "
            "read_day_source, read_day_mode, watermark_value, result, reason_code, reason, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,'accepted','','',COALESCE(?, datetime('now')))",
            (
                row["id"],
                row["run_code"],
                row["table_name"],
                row["biz_date"],
                row["biz_date"],
                READ_DAY_SOURCE,
                READ_DAY_MODE,
                row["watermark_value"],
                row["stamped_at"],
            ),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('stamp_audit_backfilled','1')"
    )


def resolve_read_day(conn: sqlite3.Connection) -> tuple[str, str]:
    """库内读日：meta 优先，其次 max(biz_date)，并把回退值落库同源。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (READ_DAY_META_KEY,)
    ).fetchone()
    if row and row["value"]:
        return row["value"], READ_DAY_SOURCE
    latest = conn.execute("SELECT MAX(biz_date) AS d FROM run_batches").fetchone()["d"]
    day = latest or READ_DAY_DEFAULT
    day = parse_day(day)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
        (READ_DAY_META_KEY, day),
    )
    return day, READ_DAY_FALLBACK_SOURCE


def set_read_day(day: str, db_path: str | None = None) -> tuple[str, str]:
    value = parse_day(day)
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
            (READ_DAY_META_KEY, value),
        )
    return value, READ_DAY_SOURCE


def read_day_info(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        day, source = resolve_read_day(conn)
    return {
        "read_day": day,
        "read_day_source": source,
        "read_day_mode": READ_DAY_MODE,
        "read_day_mode_label": "提交瞬间读库",
    }


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            for table, col, ver, status in SEED_TEMPLATES:
                conn.execute(
                    "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                    "VALUES (?,?,?,?)",
                    (table, col, ver, status),
                )
            for table, code, biz, status, wm in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        table,
                        code,
                        biz,
                        status,
                        wm,
                        "2026-11-13 18:00:00" if status == "已盖印" else None,
                    ),
                )
            for up, down in SEED_EDGES:
                conn.execute(
                    "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                    (up, down),
                )
            seeded = conn.execute(
                "SELECT * FROM run_batches WHERE run_code='ord-20261113-01'"
            ).fetchone()
            if seeded:
                conn.execute(
                    "INSERT INTO stamp_audit(batch_id, run_code, table_name, biz_date, read_day, "
                    "read_day_source, read_day_mode, watermark_value, result, reason_code, reason, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,'accepted','','','2026-11-13 18:00:00')",
                    (
                        seeded["id"],
                        seeded["run_code"],
                        seeded["table_name"],
                        seeded["biz_date"],
                        seeded["biz_date"],
                        READ_DAY_SOURCE,
                        READ_DAY_MODE,
                        seeded["watermark_value"],
                    ),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        resolve_read_day(conn)
