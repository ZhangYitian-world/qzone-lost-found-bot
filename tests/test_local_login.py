from urllib.parse import urlparse


def test_local_login_link_is_single_use_and_keeps_long_lived_token_private(client, cfg):
    link = (cfg.data_dir / "local-login.url").read_text().strip()
    route = urlparse(link).path + "?" + urlparse(link).query
    assert cfg.admin_token not in link
    first = client.get(route, follow_redirects=False)
    assert first.status_code == 303
    assert "HttpOnly" in first.headers["set-cookie"]
    assert client.get(route, follow_redirects=False).status_code == 401


def test_openapi_schema_requires_admin(client, admin):
    assert client.get('/api/openapi.json').status_code == 401
    assert client.get('/api/openapi.json', headers=admin).status_code == 200
