from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.domain import parse_day
from app.services import (
    cross_day_batches,
    get_batch,
    list_batches,
    list_reports,
    list_templates,
    read_day_info,
    run_gap_report,
    set_read_day,
    stamp_batch,
    trace_downstream,
    waterline,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def error_response(exc: ServiceError) -> Response:
    payload = {"ok": False, "error": exc.message}
    if exc.detail:
        payload["detail"] = exc.detail
    return json_response(payload, exc.status_code)


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    info = read_day_info()
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
            **info,
        }
    )


@app.router.get("/api/read-day")
async def api_read_day(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    return json_response(read_day_info())


@app.router.post("/api/read-day")
async def api_set_read_day(request: Request):
    try:
        require_user(request)
        body = await request.json()
        day = str(body.get("read_day", ""))
        try:
            parse_day(day)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        set_read_day(day)
    except ServiceError as exc:
        return error_response(exc)
    return json_response({"ok": True, **read_day_info()})


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    status = request.query.get("status")
    scope = request.query.get("scope")
    status_val = status[0] if status else None
    scope_val = scope[0] if scope else None
    return json_response(list_batches(status=status_val, scope=scope_val))


@app.router.get("/api/batches/cross-day")
async def api_cross_day(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    return json_response(cross_day_batches())


@app.router.get("/api/waterline")
async def api_waterline(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    return json_response(waterline())


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
    except ServiceError as exc:
        return error_response(exc)
    except ValueError:
        return json_response({"error": "批次不存在"}, 404)
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return error_response(exc)
    except ValueError:
        return json_response({"ok": False, "error": "批次不存在"}, 404)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await request.json()
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return error_response(exc)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return error_response(exc)
    return json_response(list_reports())


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
        body = await request.json()
        report = run_gap_report(str(body.get("as_of_date", "")))
    except ServiceError as exc:
        return error_response(exc)
    return json_response({"ok": True, "report": report})
