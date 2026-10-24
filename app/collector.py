import hashlib
import threading
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

from app.db import TZ, dumps, iso, now
from app.models import PostInput
from app.service import content_digest, uid


class AccessPaused(RuntimeError):
    pass


class CoverageUnknown(RuntimeError):
    pass


class Bridge:
    """Only read actions are exposed; no login cookies are stored in this application."""

    def __init__(self, settings):
        self.settings = settings
        self.request_lock = threading.Lock()

    def call(self, action, params):
        with self.request_lock:
            return self._call(action, params)

    def _call(self, action, params):
        if action not in ("get_emotion_list", "check_cookie", "get_login_info"):
            raise ValueError("只允许只读桥接动作")
        token_file = self.settings.data_dir / "bridge-token"
        token = self.settings.bridge_token or (token_file.read_text().strip() if token_file.exists() else "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with httpx.Client(timeout=45, trust_env=False) as client:
                r = client.post(f"{self.settings.bridge_base_url.rstrip('/')}/{action}", json=params, headers=headers)
                if r.status_code in (401, 403):
                    raise AccessPaused("桥接登录或访问权限失效，请人工处理")
                r.raise_for_status()
                obj = r.json()
        except httpx.HTTPError as exc:
            # Do not persist URLs, response bodies, cookies or Authorization headers.
            raise RuntimeError(f"桥接网络请求失败（{type(exc).__name__}）") from None
        if obj.get("retcode") in (1401, 1403) or any(w in str(obj.get("message", "")) for w in ("验证码", "无权限", "拒绝")):
            raise AccessPaused("账号异常、权限拒绝或需要登录验证，请人工处理")
        if obj.get("status") != "ok" or obj.get("retcode") != 0 or not isinstance(obj.get("data"), dict):
            raise RuntimeError("桥接业务响应异常，请检查桥接服务的诊断记录")
        return obj["data"]

    def page(self, qzone_id, cursor=None):
        p = {"user_id": qzone_id, "pos": 0, "num": 50, "max_pages": 1, "include_image_data": False}
        if cursor:
            p["cursor"] = cursor
        result = self.call("get_emotion_list", p)
        if not isinstance(result.get("msglist"), list) or not isinstance(result.get("has_more"), bool):
            raise CoverageUnknown("桥接缺少列表或明确的分页标记")
        if result.get("warnings") or result.get("reliability") != "strict":
            raise CoverageUnknown("桥接报告读取降级或部分结果，不能验证覆盖")
        return result

    def check(self):
        data = self.call("check_cookie", {"probe": True})
        if data.get("valid") is not True:
            raise AccessPaused("QQ 会话不可用，请在桥接服务中人工重新登录")
        return {"valid": True}


def normalize(raw, qzone_id):
    tid = raw.get("tid")
    if not tid:
        raise CoverageUnknown("列表缺少稳定动态标识，先留待人工导入与排查")
    timestamp = raw.get("created_time") or raw.get("createdTime") or raw.get("createTime")
    if isinstance(timestamp, dict):
        timestamp = timestamp.get("time")
    published = None
    try:
        if isinstance(timestamp, (int, float)) or (isinstance(timestamp, str) and timestamp.isdigit()):
            numeric = float(timestamp)
            published = datetime.fromtimestamp(numeric / 1000 if numeric > 1e12 else numeric, TZ)
        elif isinstance(timestamp, str):
            value = datetime.fromisoformat(timestamp)
            if value.tzinfo:
                published = value.astimezone(TZ)
    except (ValueError, OverflowError, OSError):
        pass
    meta = raw.get("picsMeta") or []
    pictures = ([p.get("originalUrl") or p.get("url") for p in meta]
                if meta and all(p.get("originalUrl") or p.get("url") for p in meta)
                else raw.get("pic") or raw.get("pics") or [])
    images = []
    for p in pictures:
        image = p if isinstance(p, str) else p.get("url2") or p.get("url3") or p.get("url1") or p.get("url")
        if not image:
            raise CoverageUnknown("图片字段结构异常")
        images.append("https:" + image if image.startswith("//") else image)
    text = raw.get("content")
    if not isinstance(text, str):
        raise CoverageUnknown("正文字段缺失或页面结构变化")
    return PostInput(external_id=str(tid), published_at=published, raw_time=str(timestamp), text=text,
                     publisher=str(raw.get("uin") or qzone_id),
                     original_url=f"https://user.qzone.qq.com/{quote(qzone_id)}/mood/{quote(str(tid), safe='')}",
                     images=images, forward_text=raw.get("forwardContent"),
                     original_ref=(f"{raw['forwardUin']}:{raw['forwardTid']}"
                                   if raw.get("forwardUin") and raw.get("forwardTid") else None),
                     content_complete=not bool(raw.get("truncated") or raw.get("is_truncated")),
                     video_unreadable=bool(raw.get("video") or raw.get("videos") or raw.get("video_url") or
                                           raw.get("feed_type") == "video"))


class Collector:
    def __init__(self, db, settings, service, bridge=None):
        self.db, self.settings, self.service = db, settings, service
        self.bridge = bridge or Bridge(settings)
        self.locks = {}

    def request(self, source_id, until=None):
        end = iso(until or now())
        with self.db.connect() as c:
            row = c.execute("SELECT enabled FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise ValueError("来源不存在")
            if not row["enabled"]:
                raise ValueError("请先配置并启用来源")
            c.execute("UPDATE sources SET pending_until=MAX(COALESCE(pending_until,''),?) WHERE id=?",
                      (end, source_id))
        return {"queued_until": end}

    def run(self, source_id):
        lock = self.locks.setdefault(source_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            self._run(source_id)
        finally:
            lock.release()

    def _run(self, source_id):
        source = self.db.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source or not source["enabled"] or not source["pending_until"]:
            return
        start, end = source["success_until"] or source["initial_since"], source["pending_until"]
        if end <= start:
            with self.db.connect() as c:
                c.execute("UPDATE sources SET pending_until=NULL WHERE id=? AND pending_until<=?", (source_id, end))
            return
        bid, log, count = uid(), [], 0
        with self.db.connect() as c:
            c.execute("INSERT INTO batches(id,source_id,window_start,window_end,started_at,coverage) "
                      "VALUES(?,?,?,?,?,'running')", (bid, source_id, start, end, iso()))
            c.execute("UPDATE sources SET last_attempt=? WHERE id=?", (iso(), source_id))
        try:
            self.bridge.check()
            recovery = iso(datetime.fromisoformat(start) - timedelta(minutes=self.settings.recovery_minutes))
            first = self.scan(source, end, recovery, bid, start, log)
            count += first["new"]
            if not source["pagination_verified"]:
                raise CoverageUnknown("尚未完成人工分页验证；已留存抓到的数据，成功截止时间保持不变")
            # A second traversal uses the same upper time bound. Set AND content comparison
            # catches inserts, moves and edits. It is evidence, not a universal completeness proof.
            second = self.scan(source, end, recovery, bid, start, log)
            count += second["new"]
            if first["signature"] != second["signature"]:
                third = self.scan(source, end, recovery, bid, start, log)
                count += third["new"]
                if second["signature"] != third["signature"]:
                    raise CoverageUnknown("固定时间上限下连续复查的标识或内容仍不一致")
                first = third
            boundary = first["boundary"]
            with self.db.connect() as c:
                c.execute("UPDATE batches SET coverage='verified',ended_at=?,page_log=?,boundary_ids=?,new_count=? "
                          "WHERE id=?", (iso(), dumps(log), dumps(boundary), count, bid))
                c.execute("UPDATE sources SET success_until=?,boundary_ids=?,last_success=?,access_state='ready',"
                          "failures=0,last_error=NULL,pending_until=CASE WHEN pending_until<=? THEN NULL "
                          "ELSE pending_until END WHERE id=?", (end, dumps(boundary), iso(), end, source_id))
        except Exception as exc:
            paused = isinstance(exc, AccessPaused)
            coverage = "unknown" if isinstance(exc, CoverageUnknown) else "incomplete"
            message = str(exc) if isinstance(exc, (AccessPaused, CoverageUnknown)) else f"采集失败（{type(exc).__name__}）"
            with self.db.connect() as c:
                c.execute("UPDATE batches SET coverage=?,ended_at=?,page_log=?,error=?,new_count=? WHERE id=?",
                          (coverage, iso(), dumps(log), message, count, bid))
                c.execute("UPDATE sources SET access_state=?,failures=failures+1,last_error=?,"
                          "enabled=CASE WHEN ? THEN 0 ELSE enabled END,pending_until=CASE WHEN pending_until<=? "
                          "THEN NULL ELSE pending_until END WHERE id=?",
                          ("paused" if paused else "coverage_unknown" if coverage == "unknown" else "error",
                           message, paused, end, source_id))

    def scan(self, source, end, recovery, bid, window_start, log):
        cursor, seen_cursors, signatures, boundary, new = None, set(), {}, [], 0
        near_ids = []
        for page_no in range(self.settings.max_pages):
            result = self.bridge.page(source["qzone_id"], cursor)
            posts = [normalize(p, source["qzone_id"]) for p in result["msglist"]]
            times = [iso(p.published_at) for p in posts if p.published_at]
            log.append({"scan": len([x for x in log if x["page"] == 0]) + (1 if page_no == 0 else 0),
                        "page": page_no, "cursor_hash": hashlib.sha256((cursor or "").encode()).hexdigest(),
                        "count": len(posts), "has_more": result["has_more"],
                        "min_published": min(times) if times else None, "max_published": max(times) if times else None,
                        "ids_hash": hashlib.sha256(dumps(sorted(p.external_id for p in posts)).encode()).hexdigest()})
            # Persist even unparseable-time records; their existence prevents a successful boundary advance.
            visible = [p for p in posts if p.published_at is None or iso(p.published_at) <= end]
            if not near_ids:
                near_ids = [p.external_id for p in sorted((x for x in visible if x.published_at),
                                                         key=lambda x: x.published_at, reverse=True)[:20]]
            existing_ids = {r["external_id"] for r in self.db.rows(
                "SELECT external_id FROM posts WHERE source_id=?", (source["id"],))}
            relevant = [p for p in visible if p.published_at is None or iso(p.published_at) >= recovery
                        or p.external_id in existing_ids]
            imported = self.service.ingest(source["id"], relevant, bid, window_start)
            new += imported["new"]
            for p in visible:
                if p.published_at and iso(p.published_at) < recovery:
                    continue
                signatures[p.external_id] = content_digest(p.model_dump(mode="json"))
            with self.db.connect() as c:
                c.execute("UPDATE batches SET page_log=? WHERE id=?", (dumps(log), bid))
            if any(p.published_at is None for p in posts):
                raise CoverageUnknown("存在无法解析发布时间的动态")
            boundary.extend(p.external_id for p in visible if iso(p.published_at) >= recovery)
            crossed = any(iso(p.published_at) < recovery for p in posts)
            # A single old/pinned entry cannot terminate a scan. A full page must be
            # older than the recovery boundary and the source must have validated ordering.
            old_page = bool(posts) and all(iso(p.published_at) < recovery for p in posts)
            if result["has_more"] is False:
                if not posts:
                    raise CoverageUnknown("接口返回空页；尚不能证明是真实空结果")
                return {"signature": signatures, "boundary": boundary[:50] or near_ids, "new": new}
            if crossed and old_page:
                return {"signature": signatures, "boundary": boundary[:50] or near_ids, "new": new}
            next_cursor = result.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise CoverageUnknown("分页游标缺失或重复")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise CoverageUnknown("达到分页保护上限，覆盖未确认")
