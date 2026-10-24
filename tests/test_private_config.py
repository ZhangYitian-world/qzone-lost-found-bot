import pytest

from app.config import LocalConfig


def test_public_defaults_contain_no_real_accounts():
    cfg = LocalConfig()
    assert cfg.login_qq == ""
    assert all(source.qzone_id == "" for source in cfg.sources)
    assert [source.name for source in cfg.sources] == ["来源一", "来源二"]


def test_account_information_is_loaded_only_from_local_config(client, admin, cfg):
    assert cfg.local_config.login_qq == "100003"
    response = client.get('/api/status', headers=admin).json()
    assert response['login_qq'] == "100003"
    assert {s['qzone_id'] for s in response['sources']} == {"100001", "100002"}
    assert "100003" not in client.get('/').text
    assert client.get('/api/status').status_code == 401


def test_local_sources_must_have_distinct_ids_and_accounts():
    with pytest.raises(ValueError):
        LocalConfig.model_validate({'sources': [
            {'id': 'wall', 'name': '一', 'qzone_id': '100001'},
            {'id': 'rights', 'name': '二', 'qzone_id': '100001'}]})
