import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Extraction, PostInput


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "local-config.json").write_text(json.dumps({"login_qq": "100003", "sources": [
        {"id": "wall", "name": "来源一", "qzone_id": "100001"},
        {"id": "rights", "name": "来源二", "qzone_id": "100002"}]}))
    return Settings(_env_file=None, data_dir=tmp_path, admin_token="a" * 40, downstream_token="d" * 40,
                    deepseek_api_key="", model_enabled=False, scheduler_enabled=False)


@pytest.fixture
def app(cfg):
    return create_app(cfg, background=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin():
    return {"Authorization": "Bearer " + "a" * 40}


@pytest.fixture
def downstream():
    return {"Authorization": "Bearer " + "d" * 40}


@pytest.fixture
def post():
    return PostInput(external_id="p1", published_at="2026-10-24T18:00:00+08:00",
                     text="【失物招领】快递站与22栋之间发现校园卡，姓名为测试同学，放在22栋置物架。")


@pytest.fixture
def extraction():
    return Extraction.model_validate({"events": [{"event_key": "card_owner", "event_type": "found_item",
        "items": ["校园卡"], "locations": {"found": "快递站与22栋之间", "stored": "22栋置物架"},
        "persons": [{"role": "card_holder", "name": "测试同学", "evidence": [
            {"kind": "text", "quote": "姓名为测试同学"}]}],
        "evidence": [{"kind": "text", "quote": "发现校园卡"}], "reason": "有清晰姓名及招领信息"}]})


@pytest.fixture
def recognized(app, post, extraction):
    service = app.state.service
    pid = service.ingest("rights", [post])["post_ids"][0]
    service.apply_extraction(pid, 1, extraction, "test-model")
    event = app.state.db.one("SELECT * FROM events WHERE post_id=?", (pid,))
    return pid, event
