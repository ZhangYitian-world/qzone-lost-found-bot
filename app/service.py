import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.db import dumps, iso, now
from app.models import PostInput


class Conflict(ValueError):
    pass


def uid() -> str:
    return uuid.uuid4().hex


def media_identity(url):
    parts = urlsplit(url)
    # These QQ CDN access parameters rotate without changing the actual image.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in {"dis_k", "dis_t", "tm", "vuin"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))


def content_digest(body):
    semantic = {k: v for k, v in body.items() if k not in ("raw_time", "original_url", "external_id")}
    semantic["images"] = [media_identity(url) for url in body["images"]]
    return hashlib.sha256(dumps(semantic).encode()).hexdigest()


def event_identity(body):
    owners = sorted((p["role"], p.get("name") or "", p.get("identifier") or "") for p in body["persons"]
                    if p["role"] in ("owner", "card_holder"))
    return dumps([body["event_type"], sorted(body["items"]), owners])


def enqueue(c, kind, ref, version):
    c.execute("INSERT OR IGNORE INTO tasks(kind,ref,version,due_at,created_at) VALUES(?,?,?,?,?)",
              (kind, ref, version, iso(), iso()))


class Service:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def ingest(self, source_id: str, posts: list[PostInput], batch_id=None, window_start=None):
        result = {"new": 0, "updated": 0, "unchanged": 0, "post_ids": []}
        with self.db.connect() as c:
            if not c.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone():
                raise ValueError("来源不存在")
            for post in posts:
                body = post.model_dump(mode="json")
                if post.published_at:
                    body["published_at"] = iso(post.published_at)
                identity = {k: body[k] for k in ("publisher", "published_at", "text", "images")}
                external = post.external_id or "fp_" + hashlib.sha256(dumps(identity).encode()).hexdigest()
                content_hash = content_digest(body)
                old = c.execute("SELECT * FROM posts WHERE source_id=? AND external_id=?",
                                (source_id, external)).fetchone()
                if old and (old["content_hash"] == content_hash or content_digest(json.loads(old["body"])) == content_hash):
                    if old["processing_state"] == "expired":
                        result["unchanged"] += 1
                        result["post_ids"].append(old["id"])
                        continue
                    # Refresh expiring download URLs without changing the content version.
                    c.execute("UPDATE posts SET body=?,content_hash=? WHERE id=?",
                              (dumps(body), content_hash, old["id"]))
                    for i, url in enumerate(body["images"]):
                        c.execute("UPDATE media SET url=? WHERE post_id=? AND post_version=? AND position=?",
                                  (url, old["id"], old["version"], i))
                    result["unchanged"] += 1
                    result["post_ids"].append(old["id"])
                    continue
                pid, version = (old["id"], old["version"] + 1) if old else (uid(), 1)
                late = bool(window_start and post.published_at and iso(post.published_at) < window_start)
                if old:
                    c.execute("UPDATE posts SET version=?,content_hash=?,body=?,collected_at=?,batch_id=?,"
                              "processing_state='pending',published_at=? WHERE id=?",
                              (version, content_hash, dumps(body), iso(), batch_id, body["published_at"], pid))
                    for ev in c.execute("SELECT * FROM events WHERE merged_into IS NULL AND id IN "
                                        "(SELECT event_id FROM event_sources WHERE post_id=?)", (pid,)).fetchall():
                        self._save_event(c, dict(ev), json.loads(ev["body"]), "pending", ev["identity_level"],
                                         "原文有新版本，等待重新核实", force=True)
                    result["updated"] += 1
                else:
                    c.execute("INSERT INTO posts(id,source_id,external_id,fingerprint_risk,version,content_hash,"
                              "published_at,collected_at,body,batch_id,late_arrival) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (pid, source_id, external, not bool(post.external_id), version, content_hash,
                               body["published_at"], iso(), dumps(body), batch_id, late))
                    result["new"] += 1
                c.execute("INSERT INTO post_versions VALUES(?,?,?,?,?)",
                          (pid, version, dumps(body), content_hash, iso()))
                # Reuse identical downloaded media without issuing another request.
                for i, url in enumerate(post.images):
                    mid = uid()
                    candidates = c.execute("SELECT path,mime,url FROM media WHERE post_id=? AND state='ready' "
                                           "ORDER BY post_version DESC", (pid,)).fetchall()
                    cached = next((m for m in candidates if media_identity(m["url"]) == media_identity(url)
                                   and m["path"] and (self.settings.data_dir / "media" / m["path"]).is_file()), None)
                    c.execute("INSERT INTO media(id,post_id,post_version,position,url,path,mime,state) "
                              "VALUES(?,?,?,?,?,?,?,?)", (mid, pid, version, i, url,
                              cached["path"] if cached else None, cached["mime"] if cached else None,
                              "ready" if cached else "pending"))
                    if not cached:
                        enqueue(c, "media", mid, version)
                enqueue(c, "recognize", pid, version)
                result["post_ids"].append(pid)
        return result

    def validate_evidence(self, c, post, event, manual=False):
        body = json.loads(post["body"])
        text = body["text"] + "\n" + (body.get("forward_text") or "")
        all_evidence = event.evidence + [e for p in event.persons for e in p.evidence]
        for e in all_evidence:
            if e.kind == "manual":
                if not manual:
                    raise ValueError("模型不能生成手工核实证据")
            elif e.kind == "text":
                if e.quote not in text:
                    raise ValueError("证据引文不在可见原文中")
            elif e.kind == "image":
                if e.image_index is None or not e.location:
                    raise ValueError("图片证据须有图片序号及文字位置")
                media = c.execute("SELECT id FROM media WHERE post_id=? AND post_version=? AND position=? "
                                  "AND state='ready'", (post["id"], post["version"], e.image_index)).fetchone()
                if not media:
                    raise ValueError("图片证据尚不可读取")
                path = c.execute("SELECT path FROM media WHERE id=?", (media["id"],)).fetchone()["path"]
                if not path or not (self.settings.data_dir / "media" / path).is_file():
                    raise ValueError("图片证据文件缺失，请先补抓")
        # Text-derived identity must itself occur in its own cited evidence.
        for p in event.persons:
            for value in (p.name, p.identifier):
                if value and p.evidence and all(e.kind == "text" for e in p.evidence):
                    if not any(value in e.quote for e in p.evidence):
                        raise ValueError("人物字段与其引用文字不一致")

    def qualify(self, c, post, event, manual=False):
        self.validate_evidence(c, post, event, manual)
        if event.event_type == "resolved":
            if not event.evidence or event.conflicts:
                raise ValueError("结案须有可读取的明确证据，且不存在未解决的冲突")
            return "closed", "I0"
        owners = [p for p in event.persons if p.role in ("owner", "card_holder") and p.evidence]
        level = "I2" if any(p.identifier for p in owners) else "I1" if any(p.name for p in owners) else "I0"
        if manual and level != "I0":
            level = "I3"
        body = json.loads(post["body"])
        media = c.execute("SELECT path FROM media WHERE post_id=? AND post_version=? AND state='ready'",
                          (post["id"], post["version"])).fetchall()
        ready_images = sum(bool(m["path"] and (self.settings.data_dir / "media" / m["path"]).is_file()) for m in media)
        complete = body["content_complete"] and not body["video_unreadable"] and ready_images == len(body["images"])
        eligible = (event.event_type in ("lost_item", "found_item") and level != "I0" and complete
                    and not event.conflicts and event.evidence and event.items)
        return ("ready" if eligible else "pending"), level

    def apply_extraction(self, pid, version, extraction, model_version):
        with self.db.connect() as c:
            post = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
            if not post or post["version"] != version or post["processing_state"] == "expired":
                return False
            keys = [event.event_key for event in extraction.events]
            if len(set(keys)) != len(keys):
                raise ValueError("同一动态的 event_key 必须唯一")
            # Validate the whole output before writing any event.
            existing = {e["event_key"]: dict(e) for e in c.execute(
                "SELECT e.* FROM events e WHERE post_id=? ORDER BY "
                "(SELECT MIN(created_at) FROM event_history h WHERE h.event_id=e.id)", (pid,))}
            # Local model keys are hints, not durable IDs. Reuse a uniquely matching
            # owner/item event, and preserve the sole event through classification changes.
            reconciled, used = [], set()
            incoming_signatures = [event_identity(e.model_dump(mode="json")) for e in extraction.events]
            for incoming in extraction.events:
                e = incoming.model_copy(deep=True)
                signature = event_identity(e.model_dump(mode="json"))
                matches = [o for o in existing.values() if not o["merged_into"] and o["event_key"] not in used
                           and event_identity(json.loads(o["body"])) == signature]
                old = matches[0] if matches and incoming_signatures.count(signature) == 1 else existing.get(e.event_key)
                if old is None and len(existing) == 1 and len(extraction.events) == 1:
                    old = next(iter(existing.values()))
                if old and old["event_key"] not in used:
                    e.event_key = old["event_key"]
                used.add(e.event_key)
                reconciled.append(e)
            keys = [e.event_key for e in reconciled]
            if len(set(keys)) != len(keys):
                raise ValueError("事件对应关系冲突，请重试或人工核实")
            qualified = [(e, *self.qualify(c, post, e)) for e in reconciled]
            for event, status, level in qualified:
                old = existing.get(event.event_key)
                body = event.model_dump(mode="json")
                if old and old["merged_into"]:
                    c.execute("UPDATE events SET proposal=? WHERE id=?", (dumps({"event": body,
                        "model_version": model_version, "post_version": version, "at": iso(),
                        "reason": "已合并来源出现新识别结果，请核对主事件"}), old["merged_into"]))
                    continue
                if old and old["human_locked"]:
                    c.execute("UPDATE events SET proposal=? WHERE id=?", (dumps({
                        "event": body, "model_version": model_version, "post_version": version,
                        "at": iso()}), old["id"]))
                    continue
                if old is None:
                    duplicate = self._explicit_duplicate(c, post, body)
                    if duplicate:
                        alias = self._save_event(c, None, body, "pending", level, "原帖关系与内容证据完全一致",
                                                 post_id=pid, model_version=model_version)
                        c.execute("UPDATE events SET merged_into=? WHERE id=?", (duplicate["id"], alias))
                        c.execute("INSERT OR IGNORE INTO event_sources VALUES(?,?)", (duplicate["id"], pid))
                        self._save_event(c, duplicate, json.loads(duplicate["body"]), duplicate["status"],
                                         duplicate["identity_level"], "追加明确原帖关系的重复来源", force=True)
                        self._audit(c, alias, "system", "merge", None, {"merged_into": duplicate["id"]},
                                    "原帖关系明确，正文、媒体和人物物品对应一致")
                        continue
                self._save_event(c, old, body, status, level, event.reason, post_id=pid,
                                 model_version=model_version)
            for key, old in existing.items():
                if key not in keys and not old["human_locked"] and not old["merged_into"]:
                    self._save_event(c, old, json.loads(old["body"]), "pending", "I0",
                                     "新识别结果不再支持旧事件")
            c.execute("UPDATE posts SET processing_state='done' WHERE id=?", (pid,))
        return True

    def _explicit_duplicate(self, c, post, body):
        raw = json.loads(post["body"])
        own_source = c.execute("SELECT qzone_id FROM sources WHERE id=?", (post["source_id"],)).fetchone()
        origin = raw.get("original_ref") or f"{own_source['qzone_id']}:{post['external_id']}"
        candidates = c.execute("SELECT e.*,p.body AS source_body,p.external_id AS source_external,s.qzone_id "
                               "FROM events e JOIN posts p ON p.id=e.post_id JOIN sources s ON s.id=p.source_id "
                               "WHERE p.source_id!=? AND e.merged_into IS NULL", (post["source_id"],)).fetchall()
        matches = []
        for row in candidates:
            other = json.loads(row["source_body"])
            if not (raw.get("original_ref") or other.get("original_ref")):
                continue
            other_origin = other.get("original_ref") or f"{row['qzone_id']}:{row['source_external']}"
            if origin != other_origin or raw["text"] != other["text"] or raw.get("forward_text") != other.get("forward_text"):
                continue
            if [media_identity(x) for x in raw["images"]] != [media_identity(x) for x in other["images"]]:
                continue
            if event_identity(body) == event_identity(json.loads(row["body"])):
                matches.append(dict(row))
        return matches[0] if len(matches) == 1 else None

    def _save_event(self, c, old, body, status, level, reason, post_id=None, model_version=None, force=False,
                    human_locked=None):
        serialized = dumps(body)
        if old and not force and (old["body"], old["status"], old["identity_level"]) == (serialized, status, level):
            return old["id"]
        eid, version = (old["id"], old["version"] + 1) if old else (uid(), 1)
        model = model_version or (old["model_version"] if old else "manual")
        locked = human_locked if human_locked is not None else old["human_locked"] if old else 0
        if old:
            c.execute("UPDATE events SET version=?,status=?,identity_level=?,body=?,model_version=?,"
                      "processed_at=?,human_locked=?,proposal=NULL WHERE id=?",
                      (version, status, level, serialized, model, iso(), locked, eid))
        else:
            c.execute("INSERT INTO events(id,post_id,event_key,version,status,identity_level,body,model_version,"
                      "processed_at,human_locked) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (eid, post_id, body["event_key"], version, status, level, serialized, model, iso(), locked))
            c.execute("INSERT INTO event_sources VALUES(?,?)", (eid, post_id))
        event = dict(c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        c.execute("INSERT INTO event_history VALUES(?,?,?,?)", (eid, version, dumps(event), iso()))
        previously_handed = bool(c.execute("SELECT 1 FROM handoffs WHERE event_id=? LIMIT 1", (eid,)).fetchone())
        # Superseded deliveries may already be in flight. The consumer must check latest version.
        c.execute("UPDATE handoffs SET state='superseded' WHERE event_id=? AND state IN ('pending','waiting','failed')",
                  (eid,))
        action = "upsert" if status == "ready" else "close" if status == "closed" else "retract"
        if status == "ready" or previously_handed:
            payload = self.payload(c, event, action, reason)
            c.execute("INSERT INTO handoffs(event_id,event_version,action,payload,next_at,created_at) "
                      "VALUES(?,?,?,?,?,?)", (eid, version, action, dumps(payload), iso(), iso()))
        return eid

    def payload(self, c, event, action, reason):
        base = {"schema_version": "1.0", "event_id": event["id"], "event_version": event["version"],
                "event_action": action, "reason": reason, "processed_at": event["processed_at"]}
        if action != "upsert":
            return base
        data = json.loads(event["body"])
        refs, evidence = [], []
        for post in c.execute("SELECT p.* FROM posts p JOIN event_sources s ON s.post_id=p.id "
                              "WHERE s.event_id=? ORDER BY p.id", (event["id"],)):
            body = json.loads(post["body"])
            batch = c.execute("SELECT window_start,window_end,coverage FROM batches WHERE id=?",
                              (post["batch_id"],)).fetchone()
            refs.append({"source_id": post["source_id"], "post_id": post["id"],
                         "external_id": post["external_id"], "post_version": post["version"],
                         "original_url": body["original_url"], "published_at": post["published_at"],
                         "collected_at": post["collected_at"], "window": dict(batch) if batch else None,
                         "fingerprint_risk": bool(post["fingerprint_risk"]),
                         "evidence_url": f"/api/evidence/posts/{post['id']}"})
            evidence.append({"kind": "post", "ref": f"/api/evidence/posts/{post['id']}"})
            for m in c.execute("SELECT id FROM media WHERE post_id=? AND post_version=? AND state='ready'",
                               (post["id"], post["version"])):
                evidence.append({"kind": "image", "ref": f"/api/evidence/media/{m['id']}"})
        base.update({k: data[k] for k in ("items", "event_time", "locations", "claim_instructions", "persons",
                                         "missing_fields", "conflicts", "event_type")})
        base.update(source_refs=refs, event_status="ready", identity_level=event["identity_level"],
                    identity_evidence=[e for p in data["persons"] if p["role"] in ("owner", "card_holder")
                                       for e in p["evidence"]], identity_confirmed=False, match_status="pending",
                    evidence_refs=evidence, review_required=True, model_version=event["model_version"])
        return base

    def correct(self, eid, correction):
        with self.db.connect() as c:
            old = self._expect(c, eid, correction.expected_version)
            if old["merged_into"]:
                raise Conflict("此事件已合并，请修改目标事件")
            if correction.event.event_key != old["event_key"]:
                raise ValueError("修正不能改变 event_key")
            post = c.execute("SELECT * FROM posts WHERE id=?", (old["post_id"],)).fetchone()
            status, level = self.qualify(c, post, correction.event, manual=True)
            self._save_event(c, old, correction.event.model_dump(mode="json"), status, level, correction.note,
                             model_version="human", force=True, human_locked=1)
            self._audit(c, eid, correction.operator, "correct", old, correction.event.model_dump(), correction.note)

    def close(self, eid, op):
        with self.db.connect() as c:
            old = self._expect(c, eid, op.expected_version)
            body = json.loads(old["body"])
            body["event_type"] = "resolved"
            self._save_event(c, old, body, "closed", old["identity_level"], op.reason,
                             model_version="human", force=True, human_locked=1)
            self._audit(c, eid, op.operator, "close", old, {"status": "closed"}, op.reason)

    def merge(self, eid, op):
        if eid == op.target_id:
            raise ValueError("不能合并到自身")
        with self.db.connect() as c:
            old = self._expect(c, eid, op.expected_version)
            target = self._expect(c, op.target_id, op.target_version)
            if old["merged_into"] or target["merged_into"]:
                raise Conflict("事件已合并，请刷新")
            self._save_event(c, old, json.loads(old["body"]), "pending", "I0", op.reason,
                             force=True, human_locked=1)
            c.execute("UPDATE events SET merged_into=? WHERE id=?", (target["id"], eid))
            c.execute("INSERT OR IGNORE INTO event_sources SELECT ?,post_id FROM event_sources WHERE event_id=?",
                      (target["id"], eid))
            self._save_event(c, target, json.loads(target["body"]), target["status"], target["identity_level"],
                             op.reason, force=True, human_locked=1)
            self._audit(c, eid, op.operator, "merge", old, {"merged_into": target["id"]}, op.reason)

    @staticmethod
    def _expect(c, eid, version):
        old = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not old:
            raise ValueError("事件不存在")
        if old["version"] != version:
            raise Conflict("版本已变化，请刷新后重试")
        return dict(old)

    @staticmethod
    def _audit(c, entity, operator, action, before, after, reason):
        c.execute("INSERT INTO audit(entity,operator,action,before_value,after_value,reason,at) VALUES(?,?,?,?,?,?,?)",
                  (entity, operator, action, dumps(before), dumps(after), reason, iso()))

    def deliveries(self, limit=50):
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM handoffs WHERE state IN ('pending','waiting') AND next_at<=? "
                             "ORDER BY seq LIMIT ?", (iso(), limit)).fetchall()
            result = []
            for r in rows:
                attempts = r["attempts"] + 1
                state = "failed" if attempts >= 8 else "waiting"
                due = iso(now() + timedelta(seconds=min(3600, 30 * 2 ** (attempts - 1))))
                c.execute("UPDATE handoffs SET state=?,attempts=?,next_at=?,last_error=? WHERE seq=?",
                          (state, attempts, due, "等待下游持久化确认", r["seq"]))
                result.append({"message_id": r["seq"], "idempotency_key":
                              f"{r['event_id']}:{r['event_version']}:{r['action']}", "payload": json.loads(r["payload"])})
            return result

    def acknowledge(self, seq):
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM handoffs WHERE seq=?", (seq,)).fetchone()
            if not row:
                raise ValueError("消息不存在")
            if row["state"] == "superseded":
                raise Conflict("消息已过期，请查询最新事件版本")
            c.execute("UPDATE handoffs SET state='acked',acked_at=COALESCE(acked_at,?),last_error=NULL WHERE seq=?",
                      (iso(), seq))
        return {"acknowledged": True, "message_id": seq}

    def event_detail(self, eid):
        event = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not event:
            raise ValueError("事件不存在")
        event["body"] = json.loads(event["body"])
        event["proposal"] = json.loads(event["proposal"]) if event["proposal"] else None
        event["posts"] = self.db.rows("SELECT p.* FROM posts p JOIN event_sources s ON s.post_id=p.id "
                                     "WHERE s.event_id=?", (eid,))
        for p in event["posts"]:
            p["body"] = json.loads(p["body"])
            p["media"] = self.db.rows("SELECT id,position,state FROM media WHERE post_id=? AND post_version=?",
                                      (p["id"], p["version"]))
        event["history"] = self.db.rows("SELECT version,created_at FROM event_history WHERE event_id=? "
                                        "ORDER BY version DESC", (eid,))
        event["audit"] = self.db.rows("SELECT operator,action,reason,at FROM audit WHERE entity=? ORDER BY id DESC",
                                      (eid,))
        event["handoffs"] = self.db.rows("SELECT seq,event_version,action,state,attempts,last_error FROM handoffs "
                                         "WHERE event_id=? ORDER BY seq DESC", (eid,))
        return event

    def reserve_budget(self):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            day = now().date().isoformat()
            spent = c.execute("SELECT COALESCE(SUM(cost),0) FROM usage WHERE day=?", (day,)).fetchone()[0]
            if spent + self.settings.model_request_reserve_cny > self.settings.daily_budget_cny:
                raise ValueError("今日预算不足，模型队列已暂停")
            return c.execute("INSERT INTO usage(day,cost,state,created_at) VALUES(?,?,?,?)",
                             (day, self.settings.model_request_reserve_cny, "reserved", iso())).lastrowid

    def settle_budget(self, reservation, input_tokens, output_tokens):
        cost = (input_tokens * self.settings.input_cny_per_million +
                output_tokens * self.settings.output_cny_per_million) / 1_000_000
        with self.db.connect() as c:
            c.execute("UPDATE usage SET cost=?,input_tokens=?,output_tokens=?,state='settled' WHERE id=?",
                      (cost, input_tokens, output_tokens, reservation))
        return cost
