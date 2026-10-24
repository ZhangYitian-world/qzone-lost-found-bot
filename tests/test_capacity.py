from app.models import PostInput


def test_500_post_storage_baseline_and_deduplication(app):
    posts = [PostInput(external_id=f"bulk-{i}", published_at="2026-10-24T18:00:00+08:00", text=f"容量样本 {i}")
             for i in range(500)]
    assert app.state.service.ingest("rights", posts)["new"] == 500
    assert app.state.service.ingest("rights", posts)["unchanged"] == 500
    assert app.state.db.one("SELECT COUNT(*) AS n FROM posts")["n"] == 500
    assert app.state.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == 500
    assert app.state.db.one("SELECT COUNT(*) AS n FROM usage")["n"] == 0
