import json
from datetime import timedelta

import pytest

from app.db import iso, now
from app.models import Correction, Extraction, Merge, Operation
from app.service import Conflict


def test_unchanged_post_dedupes_before_model(app, post):
    s, db = app.state.service, app.state.db
    assert s.ingest("rights", [post])["new"] == 1
    assert s.ingest("rights", [post])["unchanged"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM post_versions")["n"] == 1


def test_rotating_cdn_auth_parameters_do_not_trigger_new_version_or_download(app, post):
    post.images = ["https://a1.qpic.cn/psc?/image123/c&bo=800&dis_k=old&dis_t=111&tm=222&vuin=123"]
    app.state.service.ingest("rights", [post])
    post.images = ["https://a1.qpic.cn/psc?/image123/c&bo=800&dis_k=new&dis_t=333&tm=444&vuin=123"]
    assert app.state.service.ingest("rights", [post])["unchanged"] == 1
    assert app.state.db.one("SELECT version FROM posts")["version"] == 1
    assert app.state.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == 2
    assert "dis_k=new" in app.state.db.one("SELECT url FROM media")["url"]
    post.images = ["https://a1.qpic.cn/psc?/DIFFERENT_IMAGE/c&bo=800&dis_k=new&dis_t=333"]
    assert app.state.service.ingest("rights", [post])["updated"] == 1


def test_name_only_is_i1_and_no_identity_confirmation(app, recognized):
    _, event = recognized
    assert event["status"] == "ready"
    assert event["identity_level"] == "I1"
    message = json.loads(app.state.db.one("SELECT payload FROM handoffs")["payload"])
    assert message["identity_confirmed"] is False
    assert message["review_required"] is True
    assert message["locations"]["stored"] == "22栋置物架"
    assert message["persons"][0]["identifier"] is None


def test_publisher_is_not_owner(app, post, extraction):
    extraction.events[0].persons[0].role = "publisher"
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, extraction, "test")
    assert app.state.db.one("SELECT identity_level FROM events")["identity_level"] == "I0"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM handoffs")["n"] == 0


def test_invented_text_evidence_rejected_atomically(app, post, extraction):
    extraction.events[0].persons[0].evidence[0].quote = "不存在的姓名"
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    with pytest.raises(ValueError, match="原文"):
        app.state.service.apply_extraction(pid, 1, extraction, "test")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM events")["n"] == 0


def test_identity_must_occur_in_cited_text(app, post, extraction):
    extraction.events[0].persons[0].name = "杜撰名字"
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    with pytest.raises(ValueError, match="人物字段"):
        app.state.service.apply_extraction(pid, 1, extraction, "test")


@pytest.mark.parametrize("change", ["incomplete", "missing_image", "conflict", "video"])
def test_incomplete_or_conflicting_evidence_blocks_handoff(app, post, extraction, change):
    if change == "incomplete":
        post.content_complete = False
    elif change == "missing_image":
        post.images = ["https://qpic.cn/missing.jpg"]
    elif change == "conflict":
        extraction.events[0].conflicts = ["姓名对应关系无法区分"]
    else:
        post.video_unreadable = True
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, extraction, "test")
    assert app.state.db.one("SELECT status FROM events")["status"] == "pending"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM handoffs")["n"] == 0


def test_edit_retracts_previous_version_and_stale_inference_is_ignored(app, recognized, post, extraction):
    pid, event = recognized
    post.text += "（更新）"
    assert app.state.service.ingest("rights", [post])["updated"] == 1
    assert app.state.service.apply_extraction(pid, 1, extraction, "stale") is False
    messages = app.state.db.rows("SELECT * FROM handoffs ORDER BY seq")
    assert [r["action"] for r in messages] == ["upsert", "retract"]
    assert messages[0]["state"] == "superseded"
    assert messages[1]["event_version"] > event["version"]


def test_close_ack_and_old_message_cannot_overwrite(app, recognized):
    _, e = recognized
    s = app.state.service
    s.close(e["id"], Operation(expected_version=e["version"], operator="tester", reason="已核实归还"))
    messages = s.deliveries()
    assert len(messages) == 1
    assert messages[0]["payload"]["event_action"] == "close"
    assert "persons" not in messages[0]["payload"]
    seq = messages[0]["message_id"]
    assert s.acknowledge(seq) == s.acknowledge(seq)
    with pytest.raises(Conflict):
        s.acknowledge(seq - 1)


def test_repeated_inference_does_not_create_duplicate_handoff(app, recognized, extraction):
    pid, e = recognized
    app.state.service.apply_extraction(pid, 1, extraction, "test-model")
    assert app.state.db.one("SELECT version FROM events")["version"] == e["version"]
    assert app.state.db.one("SELECT COUNT(*) AS n FROM handoffs")["n"] == 1


def test_model_key_change_does_not_change_durable_event_identity(app, recognized, extraction):
    pid, event = recognized
    extraction.events[0].event_key = "different_model_generated_key"
    app.state.service.apply_extraction(pid, 1, extraction, "test-model")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM events")["n"] == 1
    assert app.state.db.one("SELECT id FROM events")["id"] == event["id"]
    assert app.state.db.one("SELECT COUNT(*) AS n FROM handoffs")["n"] == 1


def test_resolved_edit_closes_original_event_even_if_model_changes_key(app, recognized, post, extraction):
    pid, event = recognized
    post.text = "失主已领回校园卡，感谢大家。"
    app.state.service.ingest("rights", [post])
    e = extraction.events[0]
    e.event_key = "returned"
    e.event_type = "resolved"
    e.persons = []
    e.evidence[0].quote = "失主已领回校园卡"
    app.state.service.apply_extraction(pid, 2, extraction, "test-model")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM events")["n"] == 1
    assert app.state.db.one("SELECT status FROM events WHERE id=?", (event["id"],))["status"] == "closed"
    assert app.state.db.one("SELECT action FROM handoffs ORDER BY seq DESC")["action"] == "close"


def test_human_correction_locked_and_model_creates_proposal(app, recognized, extraction):
    pid, e = recognized
    s = app.state.service
    s.correct(e["id"], Correction(expected_version=e["version"], operator="tester", note="人工检查姓名",
                                 event=extraction.events[0]))
    s.apply_extraction(pid, 1, extraction, "new-model")
    current = s.event_detail(e["id"])
    assert current["human_locked"] == 1
    assert current["identity_level"] == "I3"
    assert current["proposal"]["model_version"] == "new-model"
    assert len(current["audit"]) == 1
    with pytest.raises(Conflict):
        s.close(e["id"], Operation(expected_version=1, operator="tester", reason="旧页面"))


def test_two_owners_are_separate_events(app, post, extraction):
    second = extraction.events[0].model_copy(deep=True)
    second.event_key = "second_owner"
    second.persons[0].name = "测试乙"
    second.persons[0].evidence[0].quote = "测试乙"
    post.text += "另有一张测试乙的校园卡。"
    extraction.events.append(second)
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, extraction, "test")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM events")["n"] == 2
    assert app.state.db.one("SELECT COUNT(*) AS n FROM handoffs")["n"] == 2


def test_manual_merge_preserves_sources_and_retracts_duplicate(app, recognized, post, extraction):
    _, e = recognized
    pid2 = app.state.service.ingest("wall", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid2, 1, extraction, "test")
    duplicate = app.state.db.one("SELECT * FROM events WHERE post_id=?", (pid2,))
    app.state.service.merge(duplicate["id"], Merge(expected_version=duplicate["version"], operator="tester",
        reason="核对两帖原图与原文一致，手动合并", target_id=e["id"], target_version=e["version"]))
    target = app.state.service.event_detail(e["id"])
    assert len(target["posts"]) == 2
    assert app.state.db.one("SELECT action FROM handoffs WHERE event_id=? ORDER BY seq DESC",
                            (duplicate["id"],))["action"] == "retract"


def test_explicit_repost_merges_automatically_with_one_effective_handoff(app, recognized, post, extraction):
    _, e = recognized
    post.original_ref = "100002:p1"
    post.external_id = "explicit-repost"
    pid = app.state.service.ingest("wall", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, extraction, "test-model")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM events WHERE merged_into IS NULL")["n"] == 1
    assert len(app.state.service.event_detail(e["id"])["posts"]) == 2
    assert app.state.db.one("SELECT COUNT(*) AS n FROM handoffs WHERE state='pending'")["n"] == 1


def test_same_name_and_item_alone_never_auto_merge_cross_source(app, recognized, post, extraction):
    pid = app.state.service.ingest("wall", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, extraction, "test-model")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM events WHERE merged_into IS NULL")["n"] == 2


def test_expired_unchanged_post_does_not_restore_raw_evidence(app, post, extraction):
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    with app.state.db.connect() as c:
        c.execute("UPDATE posts SET collected_at=? WHERE id=?", (iso(now()-timedelta(days=31)), pid))
    app.state.worker.cleanup()
    assert app.state.service.ingest("rights", [post])["unchanged"] == 1
    assert app.state.db.one("SELECT processing_state FROM posts")["processing_state"] == "expired"


def test_lost_ack_reuses_same_idempotency_key(app, recognized):
    first = app.state.service.deliveries()[0]
    with app.state.db.connect() as c:
        c.execute("UPDATE handoffs SET next_at=?", (iso(now() - timedelta(seconds=1)),))
    second = app.state.service.deliveries()[0]
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["message_id"] == second["message_id"]


def test_daily_budget_persists_and_blocks(app):
    app.state.service.settings.daily_budget_cny = 0.3
    app.state.service.reserve_budget()
    with pytest.raises(ValueError, match="预算"):
        app.state.service.reserve_budget()


def test_budget_settlement_accounts_for_actual_tokens(app):
    reservation = app.state.service.reserve_budget()
    app.state.service.settle_budget(reservation, 1000, 100)
    row = app.state.db.one("SELECT * FROM usage")
    assert row["state"] == "settled"
    assert row["cost"] == pytest.approx(0.00336)


def test_model_cannot_invent_manual_evidence(app, post, extraction):
    extraction.events[0].evidence[0].kind = "manual"
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    with pytest.raises(ValueError, match="手工"):
        app.state.service.apply_extraction(pid, 1, extraction, "test")


def test_model_cannot_close_without_evidence(app, post, extraction):
    extraction.events[0].event_type = "resolved"
    extraction.events[0].evidence = []
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    with pytest.raises(ValueError, match="结案"):
        app.state.service.apply_extraction(pid, 1, extraction, "test")


def test_whitespace_is_not_a_clear_name(app, post, extraction):
    payload = extraction.model_dump()
    payload['events'][0]['persons'][0]['name'] = '   '
    clean = Extraction.model_validate(payload)
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    app.state.service.apply_extraction(pid, 1, clean, "test")
    assert app.state.db.one('SELECT identity_level FROM events')['identity_level'] == 'I0'


def test_duplicate_event_keys_rejected(app, post, extraction):
    extraction.events.append(extraction.events[0].model_copy(deep=True))
    pid = app.state.service.ingest("rights", [post])["post_ids"][0]
    with pytest.raises(ValueError, match="唯一"):
        app.state.service.apply_extraction(pid, 1, extraction, "test")


def test_strict_schema_rejects_unknown_fields():
    with pytest.raises(ValueError):
        Extraction.model_validate({"events": [], "send_email": True})
