import logging
import os
import re
import socket
import time
from datetime import timedelta

from flask import Flask, g, jsonify, request, session
from psycopg import OperationalError
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

STATUSES = ("todo", "doing", "done")
TASK_FIELDS = "id, user_id, title, status, position, created_at, updated_at"
MAX_TITLE = 200
MAX_PASSWORD = 200
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOCAL_IPS = ("127.0.0.1", "::1")
APP_VERSION = os.environ.get("APP_VERSION", "v1")
POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
NODE_NAME = os.environ.get("NODE_NAME", "local")
PROBE_PATHS = frozenset(("/livez", "/api/health"))
SAFE_META_RE = re.compile(r"[^A-Za-z0-9._-]+")
PUBLIC_PATHS = frozenset((
    "/favicon.ico", "/livez",
    "/api/login", "/api/register", "/api/health",
))
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'self'"
)


def _env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError("missing required env var: " + name)
    return value


def _env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


CONN_INFO = {
    "host": _env("DB_HOST"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": _env("DB_NAME"),
    "user": _env("DB_USER"),
    "password": _env("DB_PASSWORD"),
    "sslmode": os.environ.get("DB_SSLMODE", "verify-full"),
    "connect_timeout": 15,
    "application_name": "cdb-kanban",
}
if CONN_INFO["sslmode"].startswith("verify"):
    CONN_INFO["sslrootcert"] = os.environ.get("DB_SSLROOTCERT", "system")

pool = ConnectionPool(
    kwargs=CONN_INFO,
    min_size=1,
    max_size=5,
    timeout=20,
    max_idle=120,
    max_lifetime=1800,
    reconnect_timeout=30,
    check=ConnectionPool.check_connection,
    open=False,
)
pool.open()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = _env("APP_SECRET_KEY")
app.config.update(
    SESSION_COOKIE_NAME="cdb_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env_bool("COOKIE_SECURE", True),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
class _PodContextFilter(logging.Filter):
    """Gắn thông tin Node, Pod và Version vào mỗi bản ghi log."""

    def filter(self, record):
        record.node = NODE_NAME
        record.pod = POD_NAME
        record.ver = APP_VERSION
        return True


def configure_logging():
    """Thiết lập log 1 dòng dễ theo dõi cân bằng tải giữa các Node/Pod."""
    formatter = logging.Formatter(
        "[%(asctime)s] [BE node=%(node)s pod=%(pod)s ver=%(ver)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ctx_filter = _PodContextFilter()
    app.logger.setLevel(logging.INFO)
    if not app.logger.handlers:
        app.logger.addHandler(logging.StreamHandler())
    app.logger.propagate = False
    for handler in app.logger.handlers:
        handler.setFormatter(formatter)
        handler.addFilter(ctx_filter)


def clean_meta(value, fallback=""):
    """Loại bỏ ký tự lạ khỏi metadata header trước khi ghi log."""
    cleaned = SAFE_META_RE.sub("", (value or "").strip())
    return cleaned[:64] or fallback


configure_logging()

DUMMY_HASH = generate_password_hash("cdb-kanban-dummy")


def q_all(sql, params=()):
    for attempt in (1, 2):
        try:
            with pool.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
        except OperationalError:
            if attempt == 2:
                raise


def q_one(sql, params=()):
    for attempt in (1, 2):
        try:
            with pool.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                conn.commit()
                return row
        except OperationalError:
            if attempt == 2:
                raise


def update_task_row(task_id, user_id, title=None, status=None, position=None):
    """Cập nhật title/status/position, giữ thứ tự cột liên tục trong 1 transaction."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if status is None and position is None:
                cur.execute(
                    "UPDATE tasks SET title = %s WHERE id = %s AND user_id = %s"
                    " RETURNING " + TASK_FIELDS,
                    (title, task_id, user_id),
                )
                row = cur.fetchone()
                conn.commit()
                return row

            cur.execute(
                "SELECT id, status, position FROM tasks WHERE user_id = %s"
                " ORDER BY position, id FOR UPDATE",
                (user_id,),
            )
            rows = cur.fetchall()
            current = next((r for r in rows if r["id"] == task_id), None)
            if current is None:
                return None

            target_status = status or current["status"]
            order = {
                s: [r["id"] for r in rows if r["status"] == s and r["id"] != task_id]
                for s in STATUSES
            }
            target = order[target_status]
            index = len(target) if position is None else min(position, len(target))
            target.insert(index, task_id)
            affected = {current["status"], target_status}

            cur.executemany(
                "UPDATE tasks SET status = %s, position = %s"
                " WHERE id = %s AND user_id = %s"
                " AND (status IS DISTINCT FROM %s OR position IS DISTINCT FROM %s)",
                [
                    (s, pos, rid, user_id, s, pos)
                    for s in STATUSES if s in affected
                    for pos, rid in enumerate(order[s])
                ],
            )
            if title is not None:
                cur.execute(
                    "UPDATE tasks SET title = %s WHERE id = %s AND user_id = %s",
                    (title, task_id, user_id),
                )
            cur.execute(
                "SELECT " + TASK_FIELDS + " FROM tasks WHERE id = %s AND user_id = %s",
                (task_id, user_id),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def as_int(value, field):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer" % field) from None


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def clean_title(value):
    if not isinstance(value, str):
        raise ValueError("title must be a string")
    title = value.strip()
    if not title:
        raise ValueError("title must not be empty")
    if len(title) > MAX_TITLE:
        raise ValueError("title must be at most %d characters" % MAX_TITLE)
    return title


def clean_status(value):
    if value not in STATUSES:
        raise ValueError("status must be one of: " + ", ".join(STATUSES))
    return value


def clean_position(value):
    position = as_int(value, "position")
    if position < 0:
        raise ValueError("position must be >= 0")
    return position


def task_json(row):
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "status": row["status"],
        "position": row["position"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def clean_email(value):
    if not isinstance(value, str):
        raise ValueError("email must be a string")
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValueError("email khong hop le")
    return email


def clean_password(value):
    if not isinstance(value, str):
        raise ValueError("password must be a string")
    if len(value) < 8:
        raise ValueError("mat khau phai co it nhat 8 ky tu")
    if len(value) > MAX_PASSWORD:
        raise ValueError("mat khau qua dai")
    return value


def fail(status, message, code):
    return jsonify(error=message, code=code), status


def session_user():
    uid = session.get("uid")
    if not uid:
        return None
    return {"id": uid, "email": session.get("email", "")}


def same_user(user_id):
    current = session_user()
    if current is None:
        return fail(401, "can dang nhap", "unauthenticated")
    if user_id != current["id"]:
        return fail(403, "user_id khong khop phien dang nhap", "forbidden_user")
    return None


@app.before_request
def before_request():
    g.t0 = time.perf_counter()
    if request.path in PUBLIC_PATHS:
        return None
    if not session.get("uid"):
        return fail(401, "can dang nhap", "unauthenticated")


@app.after_request
def after_request(resp):
    elapsed_ms = (time.perf_counter() - getattr(g, "t0", time.perf_counter())) * 1000
    ua = request.headers.get("User-Agent", "")
    is_healthy_probe = (
        request.path in PROBE_PATHS
        and ua.startswith("kube-probe/")
        and resp.status_code < 400
    )
    if not is_healthy_probe:
        fe_node = clean_meta(request.headers.get("X-Frontend-Node"))
        fe_pod = clean_meta(request.headers.get("X-Frontend-Pod"))
        via = f"FE:{fe_node}/{fe_pod}" if fe_node and fe_pod else (f"FE:{fe_node}" if fe_node else "direct")
        log_fn = app.logger.warning if resp.status_code >= 500 else app.logger.info
        log_fn(
            "%s %s -> %s (%.1fms) uid=%s via=%s client=%s",
            request.method,
            request.path,
            resp.status_code,
            elapsed_ms,
            session.get("uid", "-"),
            via,
            request.remote_addr,
        )
    resp.headers["X-Backend-Node"] = NODE_NAME
    resp.headers["X-Backend-Pod"] = POD_NAME
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = CSP
    if request.path.startswith("/api/") or request.path in ("/", "/login", "/register"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.path in ("/app.css", "/app.js"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.errorhandler(ValueError)
def on_value_error(err):
    return fail(400, str(err), "invalid_request")


@app.errorhandler(pg_errors.CheckViolation)
def on_check_violation(err):
    return fail(400, "du lieu khong hop le", "check_violation")


@app.errorhandler(pg_errors.UniqueViolation)
def on_unique_violation(err):
    return fail(409, "du lieu da ton tai", "unique_violation")


@app.errorhandler(pg_errors.ForeignKeyViolation)
def on_foreign_key(err):
    return fail(400, "user_id khong ton tai", "foreign_key_violation")


@app.errorhandler(OperationalError)
def on_operational(err):
    app.logger.warning("db unavailable: %s", type(err).__name__)
    return fail(503, "co so du lieu tam thoi khong san sang", "db_unavailable")


@app.errorhandler(pg_errors.Error)
def on_db_error(err):
    app.logger.error("db error: %s", type(err).__name__)
    return fail(500, "loi co so du lieu", "db_error")


@app.errorhandler(HTTPException)
def on_http_error(err):
    if request.path.startswith("/api/"):
        return fail(err.code or 500, err.description or "request failed", "http_%s" % (err.code or 500))
    return err


@app.errorhandler(Exception)
def on_unexpected(err):
    app.logger.exception("unhandled error")
    return fail(500, "loi may chu", "internal_error")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.post("/api/register")
def register():
    data = payload()
    email = clean_email(data.get("email"))
    password = clean_password(data.get("password"))
    row = q_one(
        "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id, email",
        (email, generate_password_hash(password)),
    )
    session.clear()
    session.permanent = True
    session["uid"] = row["id"]
    session["email"] = row["email"]
    return jsonify(user=row), 201


@app.post("/api/login")
def login():
    data = payload()
    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
        raise ValueError("email and password are required")
    rows = q_all(
        "SELECT id, email, password_hash FROM users WHERE lower(email) = lower(%s)",
        (email.strip(),),
    )
    stored = rows[0]["password_hash"] if rows else None
    candidate = stored or DUMMY_HASH
    valid = check_password_hash(candidate, password)
    if not rows or not stored or not valid:
        app.logger.warning("login failed ip=%s", request.remote_addr)
        return fail(401, "email hoac mat khau khong dung", "invalid_credentials")
    user = {"id": rows[0]["id"], "email": rows[0]["email"]}
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["email"] = user["email"]
    return jsonify(user=user)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    current = session_user()
    row = q_one("SELECT id, email FROM users WHERE id = %s", (current["id"],))
    if row is None:
        session.clear()
        return fail(401, "phien khong con hop le", "unauthenticated")
    return jsonify(user=row)


@app.get("/api/users")
def list_users():
    current = session_user()
    return jsonify(q_all("SELECT id, email FROM users WHERE id = %s", (current["id"],)))


@app.get("/api/tasks")
def list_tasks():
    user_id = as_int(request.args.get("user_id"), "user_id")
    denied = same_user(user_id)
    if denied:
        return denied
    rows = q_all(
        "SELECT " + TASK_FIELDS + " FROM tasks WHERE user_id = %s ORDER BY position, id",
        (user_id,),
    )
    return jsonify([task_json(row) for row in rows])


@app.post("/api/tasks")
def create_task():
    data = payload()
    user_id = as_int(data.get("user_id"), "user_id")
    denied = same_user(user_id)
    if denied:
        return denied
    title = clean_title(data.get("title"))
    row = q_one(
        "INSERT INTO tasks (user_id, title, status, position)"
        " SELECT %s, %s, 'todo', COALESCE(MAX(position), -1) + 1"
        " FROM tasks WHERE user_id = %s AND status = 'todo'"
        " RETURNING " + TASK_FIELDS,
        (user_id, title, user_id),
    )
    return jsonify(task_json(row)), 201


@app.patch("/api/tasks/<int:task_id>")
def update_task(task_id):
    data = payload()
    user_id = as_int(data.get("user_id"), "user_id")
    denied = same_user(user_id)
    if denied:
        return denied
    has_title = "title" in data
    has_status = "status" in data
    has_position = "position" in data
    if not (has_title or has_status or has_position):
        raise ValueError("nothing to update")
    title = clean_title(data.get("title")) if has_title else None
    status = clean_status(data.get("status")) if has_status else None
    position = clean_position(data.get("position")) if has_position else None
    row = update_task_row(task_id, user_id, title=title, status=status, position=position)
    if row is None:
        return fail(404, "task not found for this user", "not_found")
    return jsonify(task_json(row))


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id):
    user_id = as_int(request.args.get("user_id"), "user_id")
    denied = same_user(user_id)
    if denied:
        return denied
    row = q_one("DELETE FROM tasks WHERE id = %s AND user_id = %s RETURNING id", (task_id, user_id))
    if row is None:
        return fail(404, "task not found for this user", "not_found")
    return jsonify(row)


@app.get("/livez")
def livez():
    return jsonify(status="alive", version=APP_VERSION, pod=POD_NAME, node=NODE_NAME)


@app.get("/api/health")
def health():
    if request.args.get("detail") == "1":
        if request.remote_addr not in LOCAL_IPS:
            return fail(403, "detail is local only", "forbidden")
        detail = True
    else:
        detail = False
    try:
        row = q_all(
            "SELECT current_user AS db_user, current_database() AS database,"
            " inet_server_addr()::text AS backend_addr"
        )[0]
    except OperationalError:
        return (
            jsonify(
                status="error",
                db_user=None,
                ok=False,
                version=APP_VERSION,
                pod=POD_NAME,
                node=NODE_NAME,
                error="co so du lieu tam thoi khong san sang",
                code="db_unavailable",
            ),
            503,
        )
    if not detail:
        return jsonify(
            status="ok",
            db_user=row["db_user"],
            ok=True,
            version=APP_VERSION,
            pod=POD_NAME,
            node=NODE_NAME,
        )
    row.update(
        status="ok",
        ok=True,
        version=APP_VERSION,
        pod=POD_NAME,
        node=NODE_NAME,
        db_host=CONN_INFO["host"],
        sslmode=CONN_INFO["sslmode"],
    )
    return jsonify(row)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
