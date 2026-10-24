def test_access_roles_and_public_login_page(client, admin, downstream):
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=downstream).status_code == 403
    assert client.get("/api/status", headers=admin).status_code == 200
    assert client.post("/api/downstream/pull", headers=admin).status_code == 403
    assert client.post("/api/downstream/pull", headers=downstream).status_code == 200


def test_login_http_only_session_and_logout(client):
    r = client.post("/api/session", json={"token": "a" * 40})
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"]
    assert "SameSite=strict" in r.headers["set-cookie"]
    assert client.get("/api/status").status_code == 200
    client.delete("/api/session")
    assert client.get("/api/status").status_code == 401


def test_cross_origin_write_rejected(client, admin):
    assert client.post("/api/bridge/check", headers={**admin, "Origin": "https://other.example"}).status_code == 403


def test_import_does_not_advance_cursor(client, admin, post):
    r = client.post("/api/import", headers=admin, json={"source_id": "rights", "posts": [post.model_dump(mode="json")]})
    assert r.status_code == 200
    data = client.get("/api/status", headers=admin).json()
    assert next(s for s in data["sources"] if s["id"] == "rights")["success_until"] is None


def test_export_does_not_ack(client, admin, app, recognized):
    assert client.get("/api/handoffs/export", headers=admin).json()["messages"]
    assert app.state.db.one("SELECT state FROM handoffs")["state"] == "pending"


def test_downstream_evidence_is_scoped_to_handed_events(client, downstream, app, post, recognized):
    pid, _ = recognized
    assert client.get('/api/evidence/posts/'+pid, headers=downstream).status_code == 200
    post.external_id = "private-unrelated"
    private_pid = app.state.service.ingest("wall", [post])["post_ids"][0]
    assert client.get('/api/evidence/posts/'+private_pid, headers=downstream).status_code == 403


def test_validation_error_does_not_echo_secret(client):
    secret = "PRIVATE_SECRET_VALUE"
    r = client.post('/api/session', json={"token": {"secret": secret}})
    assert r.status_code == 422
    assert secret not in r.text


def test_schema_and_filters(client, admin, recognized):
    assert client.get('/api/schema', headers=admin).status_code == 200
    assert client.get('/api/events?identity_level=I1&status=ready&source_id=rights', headers=admin).json()["total"] == 1
    assert client.get('/api/events?identity_level=I0', headers=admin).json()["total"] == 0


def test_latest_version_and_repeat_ack(client, downstream, recognized):
    _, e = recognized
    r = client.get('/api/downstream/events/'+e["id"]+'/latest', headers=downstream)
    assert r.json()["version"] == e["version"]
    msg = client.post('/api/downstream/pull', headers=downstream).json()["messages"][0]
    for _ in range(2):
        assert client.post(f'/api/downstream/messages/{msg["message_id"]}/ack', headers=downstream).status_code == 200


def test_security_headers(client):
    r = client.get('/')
    assert r.headers['Cache-Control'] == 'no-store'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
