"""一時 URL で公開するときのアクセス制限のテスト。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from mock import access

KEY = "test-access-key-1234"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("EXAPP_ACCESS_KEY", KEY)


# --- 制限が無効なとき（ローカル開発）------------------------------------


def test_open_when_no_key_configured(monkeypatch, client):
    monkeypatch.delenv("EXAPP_ACCESS_KEY", raising=False)
    assert access.enabled() is False
    assert client.get("/ui").status_code == 200


# --- 制限が有効なとき（一時 URL 公開）-----------------------------------


def test_page_requires_a_key(client, protected):
    response = client.get("/ui")
    assert response.status_code == 401
    assert "アクセスキー" in response.text


def test_page_rejects_a_wrong_key(client, protected):
    assert client.get("/ui", params={"key": "wrong"}).status_code == 401


def test_page_accepts_the_correct_key(client, protected):
    response = client.get("/ui", params={"key": KEY})
    assert response.status_code == 200
    # 以後の診断実行のためにクッキーへ入れる
    assert access.COOKIE_NAME in response.cookies


def test_diagnosis_is_blocked_without_a_key(client, protected):
    response = client.post("/ui/run", json={"inputs": {"address": "徳島市"}})
    assert response.status_code == 401
    assert response.json()["ok"] is False


def test_diagnosis_is_blocked_with_a_wrong_key(client, protected):
    response = client.post(
        "/ui/run", params={"key": "wrong"}, json={"inputs": {"address": "徳島市"}}
    )
    assert response.status_code == 401


def test_key_comparison_is_not_prefix_based(protected):
    """前方一致で通ってしまわないこと。"""
    assert access.is_allowed(KEY[:-1]) is False
    assert access.is_allowed(KEY + "x") is False
    assert access.is_allowed(KEY) is True


def test_api_endpoint_keeps_its_own_key(client, protected):
    """画面のキーと /invoke の API キーは別物。API 側は従来どおり。"""
    response = client.post("/invoke", json={"inputs": {}}, headers={"x-api-key": "dev-local-key"})
    assert response.status_code in (200, 202)
