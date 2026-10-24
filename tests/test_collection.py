from datetime import datetime, timedelta

import pytest

from app.collector import AccessPaused, Collector, CoverageUnknown
from app.db import TZ, iso, previous_slot


BASE = datetime(2026, 10, 24, 10, tzinfo=TZ)


def raw(tid, minutes, content="测试动态"):
    return {"tid": tid, "uin": "100002", "created_time": int((BASE + timedelta(minutes=minutes)).timestamp()),
            "content": content}


def page(posts, more=False, cursor=None):
    return {"msglist": posts, "has_more": more, "next_cursor": cursor, "reliability": "strict"}


class FakeBridge:
    def __init__(self, pages, error=None):
        self.pages, self.error, self.calls = pages, error, []

    def check(self):
        if self.error:
            raise self.error
        return {"valid": True}

    def page(self, qzone_id, cursor=None):
        self.calls.append(cursor)
        return self.pages[cursor]


def configured(app, bridge, verified=True):
    with app.state.db.connect() as c:
        c.execute("UPDATE sources SET enabled=1,initial_since=?,pagination_verified=? WHERE id='rights'",
                  (iso(BASE), verified))
    collector = Collector(app.state.db, app.state.service.settings, app.state.service, bridge)
    collector.request("rights", BASE + timedelta(hours=1))
    return collector


def test_default_schedule_is_exactly_19_and_cross_day(cfg):
    assert len(cfg.hours) == 19
    assert cfg.hours == [0, 4, *range(7, 24)]
    assert previous_slot(datetime(2026, 10, 25, 3, 59, tzinfo=TZ), cfg.hours).hour == 0
    assert previous_slot(datetime(2026, 10, 25, 0, 0, tzinfo=TZ), cfg.hours).hour == 0
    assert previous_slot(datetime(2026, 10, 24, 6, 59, tzinfo=TZ), cfg.hours).hour == 4


def test_pagination_does_not_stop_at_pinned_or_first_duplicate(app):
    bridge = FakeBridge({None: page([raw("pinned", -1000), raw("a", 50)], True, "second"),
                         "second": page([raw("b", 30), raw("same-second-1", 0), raw("same-second-2", 0)], True, "third"),
                         "third": page([raw("older", -11)])})
    c = configured(app, bridge)
    c.run("rights")
    assert bridge.calls == [None, "second", "third", None, "second", "third"]
    assert app.state.db.one("SELECT COUNT(*) AS n FROM posts")["n"] == 4
    assert app.state.db.one("SELECT coverage FROM batches")["coverage"] == "verified"
    assert app.state.db.one("SELECT success_until FROM sources WHERE id='rights'")["success_until"] == iso(BASE+timedelta(hours=1))


@pytest.mark.parametrize("scenario", ["repeated_cursor", "empty", "bad_time", "max_pages", "unvalidated"])
def test_uncertain_coverage_never_advances(app, scenario):
    pages = {None: page([raw("a", 20)], True, "again"), "again": page([raw("a", 20)], True, "again")}
    if scenario == "empty":
        pages = {None: page([])}
    elif scenario == "bad_time":
        pages = {None: page([{"tid": "unknown", "content": "无法解析时间", "createTime": "昨天"}])}
    elif scenario == "unvalidated":
        pages = {None: page([raw("a", 20)])}
    c = configured(app, FakeBridge(pages), verified=scenario != "unvalidated")
    if scenario == "max_pages":
        c.settings.max_pages = 1
    c.run("rights")
    assert app.state.db.one("SELECT success_until FROM sources WHERE id='rights'")["success_until"] is None
    assert app.state.db.one("SELECT coverage FROM batches")["coverage"] == "unknown"


def test_login_failure_pauses_source_without_losing_progress(app):
    c = configured(app, FakeBridge({}, AccessPaused("请重新登录")))
    c.run("rights")
    source = app.state.db.one("SELECT * FROM sources WHERE id='rights'")
    assert source["enabled"] == 0
    assert source["success_until"] is None
    assert source["access_state"] == "paused"


def test_two_scans_disagreement_leaves_persisted_posts_for_recovery(app):
    class MovingBridge(FakeBridge):
        def page(self, qzone_id, cursor=None):
            self.calls.append(cursor)
            return page([raw(str(len(self.calls)), 20)])
    c = configured(app, MovingBridge({}))
    c.run("rights")
    assert app.state.db.one("SELECT coverage FROM batches")["coverage"] == "unknown"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM posts")["n"] == 3


def test_second_and_third_scan_can_stabilize_without_losing_first_scan_posts(app):
    class StabilizingBridge(FakeBridge):
        def page(self, qzone_id, cursor=None):
            self.calls.append(cursor)
            return page([raw("a" if len(self.calls) == 1 else "b", 20)])
    c = configured(app, StabilizingBridge({}))
    c.run("rights")
    assert app.state.db.one("SELECT coverage FROM batches")["coverage"] == "verified"
    assert app.state.db.one("SELECT COUNT(*) AS n FROM posts")["n"] == 2


def test_edits_outside_recovery_window_do_not_invalidate_window_coverage(app):
    class OldEditBridge(FakeBridge):
        def page(self, qzone_id, cursor=None):
            self.calls.append(cursor)
            return page([raw("new", 20), raw("old", -50, str(len(self.calls)))])
    c = configured(app, OldEditBridge({}))
    c.run("rights")
    assert app.state.db.one("SELECT coverage FROM batches")["coverage"] == "verified"


def test_after_round_end_posts_wait_for_next_round(app):
    c = configured(app, FakeBridge({None: page([raw("future", 70), raw("current", 20)])}))
    c.run("rights")
    assert app.state.db.one("SELECT external_id FROM posts")["external_id"] == "current"


def test_coalesced_triggers_keep_largest_end(app):
    c = configured(app, FakeBridge({}))
    c.request("rights", BASE + timedelta(hours=2))
    c.request("rights", BASE + timedelta(minutes=90))
    source = app.state.db.one("SELECT pending_until FROM sources WHERE id='rights'")
    assert source["pending_until"] == iso(BASE + timedelta(hours=2))


def test_failed_round_recovers_from_last_success(app):
    bridge = FakeBridge({}, CoverageUnknown("临时分页异常"))
    c = configured(app, bridge)
    c.run("rights")
    bridge.error = None
    bridge.pages = {None: page([raw("late", 20), raw("new", 80)])}
    c.request("rights", BASE + timedelta(hours=2))
    c.run("rights")
    batches = app.state.db.rows("SELECT * FROM batches ORDER BY started_at")
    assert batches[1]["window_start"] == iso(BASE)
    assert batches[1]["coverage"] == "verified"


def test_late_old_post_is_marked_catchup(app):
    c = configured(app, FakeBridge({None: page([raw("late", -5), raw("new", 20)])}))
    c.run("rights")
    assert app.state.db.one("SELECT late_arrival FROM posts WHERE external_id='late'")["late_arrival"] == 1
