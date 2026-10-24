import json
from datetime import timedelta

import pytest

from app.db import iso, now
from app.worker import safe_media_url


def test_no_api_key_keeps_recognition_pending(app, post):
    app.state.service.ingest("rights", [post])
    app.state.worker.tick()
    assert app.state.db.one("SELECT state FROM tasks")["state"] == "pending"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM usage")["n"] == 0


@pytest.mark.parametrize("url", ["http://qpic.cn/a.jpg", "https://127.0.0.1/a.jpg", "https://qpic.cn.evil.test/a.jpg",
                                 "file:///etc/passwd", "https://qpic.cn:8080/a.jpg", "https://secret@qpic.cn/a.jpg"])
def test_media_url_allowlist_blocks_unsafe_urls(url):
    with pytest.raises(ValueError):
        safe_media_url(url, ["qpic.cn"])


def test_media_failure_keeps_independent_retry_task(app, post, monkeypatch):
    post.images = ["https://qpic.cn/unavailable.jpg"]
    app.state.service.ingest("rights", [post])
    def fail(task):
        raise RuntimeError("Authorization: secret should never be logged")
    monkeypatch.setattr(app.state.worker, "download_media", fail)
    app.state.worker.tick()
    task = app.state.db.one("SELECT * FROM tasks WHERE kind='media'")
    assert task["state"] == "pending"
    assert task["attempts"] == 1
    assert "secret" not in task["error"]
    assert app.state.db.one("SELECT state FROM tasks WHERE kind='recognize'")["state"] == "pending"


def test_expired_evidence_retracts_and_blocks_access(app, post, extraction, client, downstream):
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, extraction, "test")
    with app.state.db.connect() as c:
        c.execute("UPDATE posts SET collected_at=? WHERE id=?", (iso(now()-timedelta(days=31)), pid))
    app.state.worker.cleanup()
    assert app.state.db.one("SELECT status FROM events")["status"] == "pending"
    assert app.state.db.one("SELECT action FROM handoffs ORDER BY seq DESC")["action"] == "retract"
    assert client.get('/api/evidence/posts/'+pid, headers=downstream).status_code == 410
    body = json.loads(app.state.db.one("SELECT body FROM posts")["body"])
    assert "测试同学" not in body["text"]
    assert json.loads(app.state.db.one("SELECT body FROM events")["body"])["evidence"] == []


def test_exhausted_media_creates_uncertain_case_without_calling_model(app, post, monkeypatch):
    post.images = ["https://qpic.cn/missing.jpg"]
    app.state.service.ingest("rights", [post])
    def fail(task):
        raise ValueError("图片不可读取")
    monkeypatch.setattr(app.state.worker, "download_media", fail)
    with app.state.db.connect() as c:
        c.execute("UPDATE tasks SET attempts=3 WHERE kind='media'")
    app.state.worker.tick()
    event = app.state.db.one("SELECT body,status FROM events")
    assert json.loads(event["body"])["event_type"] == "uncertain"
    assert event["status"] == "pending"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM usage")["n"] == 0
