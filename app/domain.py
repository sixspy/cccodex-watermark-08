"""跨日水线与读日口径的纯领域常量/函数。

所有视图（跨日列表、盖印详情、批次水线、追索台）都必须从这里取同一份
读日与判定口径，避免各自写一套日期比较，导致“判定一套、提示一套”。
"""

from datetime import date

# ---- 读日来源 ----
READ_DAY_META_KEY = "current_biz_date"
READ_DAY_SOURCE = "meta.current_biz_date"
READ_DAY_FALLBACK_SOURCE = "run_batches.max(biz_date)"
READ_DAY_DEFAULT = "2026-11-14"

# ---- 读日时机：提交瞬间读库（而非打开页面时冻结快照） ----
READ_DAY_MODE = "submit"
READ_DAY_MODE_LABEL = "提交瞬间读库"

# ---- 批次状态 ----
STATUS_PENDING = "待盖印"
STATUS_STAMPED = "已盖印"
STATUS_VOID = "已作废"

# ---- 日关系 ----
DAY_SAME = "当日"
DAY_CROSS = "跨日"
DAY_FUTURE = "未来日"

# ---- 拒因短句（页面直接展示，库内同句留证） ----
REASON_CROSS_DAY = "跨日批次不可当日盖印"
REASON_FUTURE = "批次业务日晚于读日"
REASON_ALREADY_STAMPED = "批次已盖印"
REASON_VOID = "批次已作废"
REASON_NOT_PENDING = "仅待盖印批次可盖印"
REASON_NO_TEMPLATE = "无现行水印模板"
REASON_EMPTY_VALUE = "水印值不能为空"
REASON_NOT_FOUND = "批次不存在"
REASON_NO_VALID_SOURCE = "未找到该水印值的有效盖印批次"
REASON_BAD_DAY = "业务日格式应为 YYYY-MM-DD"
REASON_CROSS_DAY_WRITE = "跨日写入已被拦截"

# ---- 唯一有效盖印口径：同日接受留证 ----
VALID_STAMP_SQL = (
    "EXISTS (SELECT 1 FROM stamp_audit a WHERE a.batch_id = b.id "
    "AND a.result = 'accepted' AND a.biz_date = a.read_day)"
)

RELATION_CODES = {
    DAY_SAME: "same_day",
    DAY_CROSS: "cross_day",
    DAY_FUTURE: "future_day",
}

REASON_BY_CODE = {
    "void": REASON_VOID,
    "already_stamped": REASON_ALREADY_STAMPED,
    "cross_day": REASON_CROSS_DAY,
    "future_day": REASON_FUTURE,
    "not_pending": REASON_NOT_PENDING,
    "no_template": REASON_NO_TEMPLATE,
    "empty_value": REASON_EMPTY_VALUE,
}


def day_relation(biz_date: str, read_day: str) -> str:
    """业务日相对读日的关系：只在同一份口径下比较。"""
    if biz_date == read_day:
        return DAY_SAME
    if biz_date < read_day:
        return DAY_CROSS
    return DAY_FUTURE


def parse_day(value: str) -> str:
    """把业务日/读日规整成 YYYY-MM-DD，格式不对抛 ValueError(短句)。"""
    text = (value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(REASON_BAD_DAY) from exc


def _gate(allowed: bool, relation: str, code: str, reason: str) -> dict:
    return {
        "allowed": allowed,
        "relation": relation,
        "reason_code": code,
        "reason": reason,
    }


def evaluate_stamp_gate(
    biz_date: str,
    status: str,
    read_day: str,
    has_template: bool = True,
) -> dict:
    """一次判定：状态优先，其次日关系，最后模板。

    跨日/未来日一律拒，跨日批次绝不允许写 run_batches。
    """
    relation = day_relation(biz_date, read_day)
    if status == STATUS_VOID:
        return _gate(False, relation, "void", REASON_VOID)
    if status == STATUS_STAMPED:
        return _gate(False, relation, "already_stamped", REASON_ALREADY_STAMPED)
    if relation == DAY_CROSS:
        return _gate(False, relation, "cross_day", REASON_CROSS_DAY)
    if relation == DAY_FUTURE:
        return _gate(False, relation, "future_day", REASON_FUTURE)
    if status != STATUS_PENDING:
        return _gate(False, relation, "not_pending", REASON_NOT_PENDING)
    if not has_template:
        return _gate(False, relation, "no_template", REASON_NO_TEMPLATE)
    return _gate(True, relation, "", "")
