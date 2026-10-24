import socket

import pytest

from app.worker import safe_media_url


def resolved(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


def test_proxy_fake_ip_is_opt_in_and_still_restricted_to_trusted_domains(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: resolved("198.18.2.46"))
    with pytest.raises(ValueError):
        safe_media_url("https://a1.qpic.cn/photo", ["qpic.cn"])
    safe_media_url("https://a1.qpic.cn/photo", ["qpic.cn"], allow_fake_ip=True)
    with pytest.raises(ValueError):
        safe_media_url("https://evil.example/photo", ["qpic.cn"], allow_fake_ip=True)


def test_fake_ip_option_does_not_allow_other_private_addresses(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: resolved("192.168.1.1"))
    with pytest.raises(ValueError):
        safe_media_url("https://a1.qpic.cn/photo", ["qpic.cn"], allow_fake_ip=True)
