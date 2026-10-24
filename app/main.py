import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.collector import Collector
from app.config import Settings
from app.db import Database, iso, now
from app.models import Correction, ImportInput, Merge, Operation, SourceUpdate
from app.service import Conflict, Service, enqueue
from app.worker import Worker

STATIC = Path(__file__).parent / "static"


def create_app(settings=None, background=True):
    cfg = settings or Settings()
    if len(cfg.admin_token) < 24 or len(cfg.downstream_token) < 24 or cfg.admin_token == cfg.downstream_token:
        raise RuntimeError("请先运行 .venv/bin/python scripts/setup.py，生成独立管理和下游令牌")
    cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    process_lock = (cfg.data_dir / "service.lock").open("a")
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        process_lock.close()
        raise RuntimeError("此数据目录已有一个服务进程运行，请使用单进程启动") from None
    db = Database(cfg.data_dir / "qzone.sqlite3")
    db.init(cfg.hours, [(s.id, s.name, s.qzone_id) for s in cfg.local_config.sources])
    service = Service(db, cfg)
    collector = Collector(db, cfg, service)
    worker = Worker(db, cfg, service)
    active_collectors = {}
    stop = asyncio.Event()
    local_ticket = secrets.token_urlsafe(32)
    ticket_hash = hashlib.sha256(local_ticket.encode()).hexdigest()
    ticket_expires = time.time() + 15 * 60
    # A short-lived, single-use convenience link, never a long-lived API token.
    link_file = cfg.data_dir / "local-login.url"
    link_file.write_text("http://127.0.0.1:8766/api/local-login?ticket=" + local_ticket + "\n")
    link_file.chmod(0o600)

    async def schedule_loop():
        while not stop.is_set():
            try:
                if cfg.scheduler_enabled:
                    worker.schedule(collector)
                for source in db.rows("SELECT id FROM sources WHERE enabled=1 AND pending_until IS NOT NULL"):
                    sid = source["id"]
                    if sid not in active_collectors or active_collectors[sid].done():
                        active_collectors[sid] = asyncio.create_task(asyncio.to_thread(collector.run, sid))
            except Exception as exc:
                logging.error("调度异常: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def work_loop():
        while not stop.is_set():
            try:
                await asyncio.to_thread(worker.cleanup)
                await asyncio.to_thread(worker.tick)
                with db.connect() as c:
                    c.execute("INSERT OR REPLACE INTO maintenance VALUES('worker_heartbeat',?)", (iso(),))
            except Exception as exc:
                logging.error("处理队列异常: %s", type(exc).__name__)
            await asyncio.sleep(3)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(schedule_loop()), asyncio.create_task(work_loop())] if background else []
        yield
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*active_collectors.values(), return_exceptions=True)
        process_lock.close()

    app = FastAPI(title="QQ空间失物线索", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.db, app.state.service, app.state.collector, app.state.worker = db, service, collector, worker
    app.state.process_lock = process_lock
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "拒绝跨站写入"}, status_code=403)
            if int(request.headers.get("content-length", "0")) > 10 * 1024 * 1024:
                return JSONResponse({"detail": "请求体超过10MB"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return response

    def session_valid(cookie):
        if not cookie:
            return False
        try:
            expires, signature = cookie.split(".")
            expected = hmac.new(cfg.admin_token.encode(), expires.encode(), hashlib.sha256).hexdigest()
            return int(expires) > time.time() and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def role(request: Request):
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            token = header[7:]
            if hmac.compare_digest(token, cfg.admin_token):
                return "admin"
            if hmac.compare_digest(token, cfg.downstream_token):
                return "downstream"
        if session_valid(request.cookies.get("qzone_session")):
            return "admin"
        raise HTTPException(401, "请登录或提供有效 Bearer 令牌")

    def admin(who=Depends(role)):
        if who != "admin":
            raise HTTPException(403, "此接口仅限操作者")

    def downstream(who=Depends(role)):
        if who != "downstream":
            raise HTTPException(403, "此接口须使用独立下游令牌")

    def authorized_post(pid, who):
        post = db.one("SELECT * FROM posts WHERE id=?", (pid,))
        if not post:
            raise HTTPException(404, "原文不存在")
        if post["processing_state"] == "expired":
            raise HTTPException(410, "原文证据已过保留期，请人工核实")
        if who == "downstream":
            allowed = db.one("SELECT 1 AS ok FROM event_sources s JOIN events e ON e.id=s.event_id "
                             "WHERE s.post_id=? AND EXISTS(SELECT 1 FROM handoffs h WHERE h.event_id=e.id) LIMIT 1",
                             (pid,))
            if not allowed:
                raise HTTPException(403, "此证据不在下游交接范围内")
        return post

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # FastAPI's default validation response echoes raw input, including secrets.
        errors = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    def add_session(response, secure=False):
        expires = str(int(time.time()) + 12 * 3600)
        signature = hmac.new(cfg.admin_token.encode(), expires.encode(), hashlib.sha256).hexdigest()
        response.set_cookie("qzone_session", f"{expires}.{signature}", httponly=True, samesite="strict",
                            secure=secure, max_age=12 * 3600)
        return response

    @app.get("/api/local-login", include_in_schema=False)
    def local_login(request: Request, ticket: str = ""):
        nonlocal ticket_hash
        if (request.client is None or request.client.host not in ("127.0.0.1", "::1", "testclient")
                or not ticket_hash or time.time() >= ticket_expires
                or not hmac.compare_digest(hashlib.sha256(ticket.encode()).hexdigest(), ticket_hash)):
            raise HTTPException(401, "一次性登录链接已失效，请使用管理令牌登录")
        ticket_hash = ""
        link_file.unlink(missing_ok=True)
        return add_session(RedirectResponse("/", status_code=303), request.url.scheme == "https")

    class Login(BaseModel):
        token: str = Field(min_length=1, max_length=500)

    login_failures = {}

    @app.post("/api/session")
    def login(body: Login, request: Request):
        ip = request.client.host if request.client else "local"
        recent = [t for t in login_failures.get(ip, []) if t > time.time() - 60]
        if len(recent) >= 10:
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试")
        if not secrets.compare_digest(body.token, cfg.admin_token):
            login_failures[ip] = recent + [time.time()]
            raise HTTPException(401, "管理令牌不正确")
        login_failures.pop(ip, None)
        return add_session(JSONResponse({"authenticated": True}), request.url.scheme == "https")

    @app.delete("/api/session")
    def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("qzone_session")
        return response

    @app.get("/api/status", dependencies=[Depends(admin)])
    def status():
        spent = db.one("SELECT COALESCE(SUM(cost),0) AS amount FROM usage WHERE day=?",
                       (now().date().isoformat(),))["amount"]
        task_counts = db.rows("SELECT kind,state,COUNT(*) AS count FROM tasks GROUP BY kind,state")
        counts = {r["status"]: r["count"] for r in db.rows(
            "SELECT status,COUNT(*) AS count FROM events WHERE merged_into IS NULL GROUP BY status")}
        alerts = []
        for s in db.rows("SELECT * FROM sources"):
            if s["failures"] >= 2:
                alerts.append(f"{s['name']} 连续 {s['failures']} 轮采集未完成")
            if s["access_state"] == "paused":
                alerts.append(f"{s['name']} 登录或权限异常，采集已暂停")
        backlog = db.one("SELECT COUNT(*) AS n FROM tasks WHERE state IN ('pending','running') AND created_at<?",
                         (iso(now() - timedelta(minutes=10)),))["n"]
        if backlog:
            alerts.append(f"{backlog} 个内容任务等待超过10分钟")
        if not cfg.deepseek_api_key:
            alerts.append("尚未配置 DeepSeek API Key，模型处理暂停，数据继续留档")
        elif not cfg.model_enabled:
            alerts.append("模型调用已在配置中暂停")
        if spent + cfg.model_request_reserve_cny > cfg.daily_budget_cny:
            alerts.append("今日预算不足以预留下一次调用，模型处理暂停")
        failed = db.one("SELECT COUNT(*) AS n FROM handoffs WHERE state='failed'")["n"]
        if failed:
            alerts.append(f"{failed} 条交接消息多次未确认，可手工重试")
        return {"sources": db.rows("SELECT * FROM sources ORDER BY id"), "events": counts,
                "login_qq": cfg.local_config.login_qq,
                "posts": db.one("SELECT COUNT(*) AS n FROM posts")["n"], "tasks": task_counts,
                "handoff_backlog": db.one("SELECT COUNT(*) AS n FROM handoffs "
                                          "WHERE state IN ('pending','waiting','failed')")["n"],
                "spent_cny": round(spent, 4), "budget_cny": cfg.daily_budget_cny,
                "model": cfg.deepseek_model, "model_configured": bool(cfg.deepseek_api_key),
                "model_enabled": cfg.model_enabled, "schedule_hours": cfg.hours, "timezone": "Asia/Shanghai",
                "worker_heartbeat": db.one("SELECT value FROM maintenance WHERE key='worker_heartbeat'"),
                "alerts": alerts, "now": iso()}

    @app.put("/api/sources/{sid}", dependencies=[Depends(admin)])
    def update_source(sid: str, body: SourceUpdate):
        if body.pagination_verified and not body.validation_notes.strip():
            raise ValueError("标记分页已验证前，请记录同秒边界、置顶、插入、删除和乱序验证结果")
        if body.initial_since > now():
            raise ValueError("首次增量起点不能在未来")
        with db.connect() as c:
            old = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            if not old:
                raise ValueError("来源不存在")
            if body.qzone_id != old["qzone_id"] and c.execute("SELECT 1 FROM posts WHERE source_id=?", (sid,)).fetchone():
                raise ValueError("已有数据的来源不能更换 QQ 标识")
            c.execute("UPDATE sources SET name=?,qzone_id=?,enabled=?,initial_since=?,pagination_verified=?,"
                      "validation_notes=?,last_slot=NULL WHERE id=?", (body.name, body.qzone_id, body.enabled,
                      iso(body.initial_since), body.pagination_verified, body.validation_notes, sid))
            service._audit(c, sid, "local-admin", "source_config", dict(old), body.model_dump(mode="json"), "更新来源配置")
        return {"saved": True}

    @app.post("/api/sources/{sid}/collect", dependencies=[Depends(admin)])
    def collect(sid: str):
        return collector.request(sid)

    @app.post("/api/bridge/check", dependencies=[Depends(admin)])
    def check_bridge():
        try:
            return collector.bridge.check()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/import", dependencies=[Depends(admin)])
    def import_posts(body: ImportInput):
        return service.ingest(body.source_id, body.posts)

    @app.get("/api/posts", dependencies=[Depends(admin)])
    def posts(source_id: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        where, params = ("WHERE source_id=?", [source_id]) if source_id else ("", [])
        rows = db.rows(f"SELECT * FROM posts {where} ORDER BY collected_at DESC,id LIMIT ? OFFSET ?",
                       (*params, limit, offset))
        for r in rows:
            body = json.loads(r.pop("body"))
            r["preview"] = body["text"][:220]
            r["image_count"] = len(body["images"])
        return rows

    @app.get("/api/events", dependencies=[Depends(admin)])
    def events(source_id: str | None = None, status: str | None = None, identity_level: str | None = None,
               handoff_state: str | None = None, since: datetime | None = None, until: datetime | None = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        clauses, params = ["e.merged_into IS NULL"], []
        for key, value in (("e.status", status), ("e.identity_level", identity_level)):
            if value:
                clauses.append(key + "=?")
                params.append(value)
        if source_id:
            clauses.append("EXISTS(SELECT 1 FROM event_sources x JOIN posts p ON p.id=x.post_id "
                           "WHERE x.event_id=e.id AND p.source_id=?)")
            params.append(source_id)
        if handoff_state:
            if handoff_state == "none":
                clauses.append("NOT EXISTS(SELECT 1 FROM handoffs h WHERE h.event_id=e.id)")
            else:
                clauses.append("(SELECT h.state FROM handoffs h WHERE h.event_id=e.id ORDER BY seq DESC LIMIT 1)=?")
                params.append(handoff_state)
        for value, relation in ((since, ">="), (until, "<=")):
            if value:
                if value.tzinfo is None:
                    raise ValueError("时间筛选须包含时区")
                clauses.append(f"e.processed_at{relation}?")
                params.append(iso(value))
        where = " AND ".join(clauses)
        rows = db.rows(f"SELECT e.*,(SELECT state FROM handoffs h WHERE h.event_id=e.id ORDER BY seq DESC LIMIT 1) "
                       f"AS handoff_state FROM events e WHERE {where} ORDER BY e.processed_at DESC,e.id "
                       "LIMIT ? OFFSET ?", (*params, limit, offset))
        for r in rows:
            r["body"] = json.loads(r["body"])
            r["proposal"] = bool(r["proposal"])
        return {"items": rows, "total": db.one(f"SELECT COUNT(*) AS n FROM events e WHERE {where}", params)["n"]}

    @app.get("/api/events/{eid}", dependencies=[Depends(admin)])
    def detail(eid: str):
        return service.event_detail(eid)

    @app.put("/api/events/{eid}", dependencies=[Depends(admin)])
    def correct(eid: str, body: Correction):
        service.correct(eid, body)
        return service.event_detail(eid)

    @app.post("/api/events/{eid}/close", dependencies=[Depends(admin)])
    def close(eid: str, body: Operation):
        service.close(eid, body)
        return {"closed": True}

    @app.post("/api/events/{eid}/merge", dependencies=[Depends(admin)])
    def merge(eid: str, body: Merge):
        service.merge(eid, body)
        return {"merged_into": body.target_id}

    @app.post("/api/posts/{pid}/reprocess", dependencies=[Depends(admin)])
    def reprocess(pid: str):
        post = authorized_post(pid, "admin")
        with db.connect() as c:
            enqueue(c, "recognize", pid, post["version"])
            c.execute("UPDATE tasks SET state='pending',attempts=0,due_at=?,error=NULL "
                      "WHERE kind='recognize' AND ref=? AND version=?", (iso(), pid, post["version"]))
            c.execute("UPDATE posts SET processing_state='pending' WHERE id=?", (pid,))
        return {"queued": True}

    @app.get("/api/tasks", dependencies=[Depends(admin)])
    def tasks():
        return db.rows("SELECT * FROM tasks WHERE state!='done' ORDER BY id LIMIT 200")

    @app.post("/api/tasks/{tid}/retry", dependencies=[Depends(admin)])
    def retry_task(tid: int):
        with db.connect() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            c.execute("UPDATE tasks SET state='pending',attempts=0,due_at=?,error=NULL WHERE id=?", (iso(), tid))
            if row["kind"] == "media":
                c.execute("UPDATE media SET state='pending' WHERE id=?", (row["ref"],))
        return {"queued": True}

    @app.get("/api/batches", dependencies=[Depends(admin)])
    def batches():
        return db.rows("SELECT * FROM batches ORDER BY started_at DESC LIMIT 100")

    @app.get("/api/handoffs/export", dependencies=[Depends(admin)])
    def export(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        rows = db.rows("SELECT seq,payload FROM handoffs WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))
        return {"messages": [{"message_id": r["seq"], "payload": json.loads(r["payload"])} for r in rows],
                "next_cursor": rows[-1]["seq"] if rows else after}

    @app.get("/api/handoffs", dependencies=[Depends(admin)])
    def handoffs():
        return db.rows("SELECT seq,event_id,event_version,action,state,attempts,last_error,created_at,acked_at "
                       "FROM handoffs ORDER BY seq DESC LIMIT 200")

    @app.post("/api/handoffs/{seq}/retry", dependencies=[Depends(admin)])
    def retry_handoff(seq: int):
        with db.connect() as c:
            row = c.execute("SELECT * FROM handoffs WHERE seq=?", (seq,)).fetchone()
            if not row:
                raise ValueError("消息不存在")
            latest = c.execute("SELECT version FROM events WHERE id=?", (row["event_id"],)).fetchone()
            if latest["version"] != row["event_version"] or row["state"] in ("acked", "superseded"):
                raise Conflict("已确认或过期消息不能重投")
            c.execute("UPDATE handoffs SET state='pending',attempts=0,next_at=?,last_error=NULL WHERE seq=?",
                      (iso(), seq))
        return {"queued": True}

    @app.post("/api/downstream/pull", dependencies=[Depends(downstream)])
    def pull(limit: int = Query(50, ge=1, le=100)):
        return {"messages": service.deliveries(limit)}

    @app.post("/api/downstream/messages/{seq}/ack", dependencies=[Depends(downstream)])
    def ack(seq: int):
        return service.acknowledge(seq)

    @app.get("/api/downstream/events/{eid}/latest", dependencies=[Depends(downstream)])
    def latest(eid: str):
        event = db.one("SELECT id,version,status,processed_at,merged_into FROM events WHERE id=? AND "
                       "EXISTS(SELECT 1 FROM handoffs h WHERE h.event_id=events.id)", (eid,))
        if not event:
            raise HTTPException(404, "事件不存在或未进入交接范围")
        return event

    @app.get("/api/evidence/posts/{pid}")
    def evidence_post(pid: str, who=Depends(role)):
        post = authorized_post(pid, who)
        return {"id": pid, "version": post["version"], "published_at": post["published_at"],
                "collected_at": post["collected_at"], "body": json.loads(post["body"])}

    @app.get("/api/evidence/media/{mid}")
    def evidence_media(mid: str, who=Depends(role)):
        media = db.one("SELECT * FROM media WHERE id=?", (mid,))
        if not media:
            raise HTTPException(404, "图片不存在")
        post = authorized_post(media["post_id"], who)
        if media["post_version"] != post["version"] or media["state"] != "ready":
            raise HTTPException(410, "图片证据失效，请人工核实")
        path = worker.media_dir / (media["path"] or "missing")
        if not path.is_file():
            raise HTTPException(410, "图片文件缺失，请补抓")
        return FileResponse(path, media_type=media["mime"])

    @app.get("/api/schema", dependencies=[Depends(admin)])
    def schema():
        from app.models import Extraction
        return Extraction.model_json_schema()

    @app.get("/api/openapi.json", dependencies=[Depends(admin)], include_in_schema=False)
    def openapi_schema():
        return app.openapi()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
