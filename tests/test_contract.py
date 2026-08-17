"""源内 Web との契約テスト。

ここが緑なら、本物の源内 Web に登録しても仕様違反では落ちない。
"""

from __future__ import annotations

import base64
import time

import pytest
from fastapi.testclient import TestClient

from app import conformance
from app.main import app
from app.normalize import normalize

HEADERS = {"x-api-key": "dev-local-key", "x-user-id": "stable-user-123"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _wait_for_completion(client, status_url: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(status_url, headers=HEADERS)
        assert response.status_code == 200
        body = response.json()
        status, problems = conformance.check_status_response(response.status_code, body)
        assert problems == [], problems
        if status in ("COMPLETED", "ERROR"):
            return body
        time.sleep(0.1)
    pytest.fail("ジョブが終了状態になりませんでした")


# --- 認証 ---------------------------------------------------------------


def test_api_key_is_required(client):
    response = client.post("/invoke", json={"inputs": {}})
    assert response.status_code == 401


def test_wrong_api_key_is_rejected(client):
    response = client.post(
        "/invoke", json={"inputs": {}}, headers={"x-api-key": "wrong"}
    )
    assert response.status_code == 401


# --- リクエスト形式 -----------------------------------------------------


def test_inputs_is_required(client):
    response = client.post("/invoke", json={"question": "inputs でラップされていない"}, headers=HEADERS)
    assert response.status_code == 400


def test_broken_json_returns_400(client):
    response = client.post(
        "/invoke", content=b"{not json", headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code == 400


# --- 同期実行 -----------------------------------------------------------


def test_sync_response_conforms(client):
    response = client.post(
        "/invoke?mode=sync", json={"inputs": {}}, headers=HEADERS
    )
    mode, problems = conformance.check_invoke_response(response.status_code, response.json())
    assert mode == "sync"
    assert problems == [], problems
    assert isinstance(response.json()["outputs"], str)


# --- 非同期実行 ---------------------------------------------------------


def test_async_accept_response_conforms(client):
    response = client.post(
        "/invoke?mode=async", json={"inputs": {}}, headers=HEADERS
    )
    assert response.status_code == 202
    mode, problems = conformance.check_invoke_response(response.status_code, response.json())
    assert mode == "async"
    assert problems == [], problems


def test_status_url_is_same_origin_relative_path(client):
    response = client.post("/invoke?mode=async", json={"inputs": {}}, headers=HEADERS)
    status_url = response.json()["status_url"]
    # 源内は絶対 URL・// 始まり・バックスラッシュを拒否する
    assert status_url.startswith("/")
    assert not status_url.startswith("//")
    assert conformance.check_status_url(status_url) == []


def test_async_completes_with_outputs_and_artifacts(client):
    response = client.post(
        "/invoke?mode=async", json={"inputs": {}}, headers=HEADERS
    )
    body = _wait_for_completion(client, response.json()["status_url"])

    assert body["status"] == "COMPLETED"
    assert isinstance(body["outputs"], str) and body["outputs"]
    for artifact in body.get("artifacts", []):
        assert artifact["display_name"]
        base64.b64decode(artifact["contents"], validate=True)


def test_unknown_request_id_terminates_instead_of_polling_forever(client):
    response = client.get("/status/does-not-exist", headers=HEADERS)
    # 404 を返すと源内は最大 96 時間ポーリングし続けるので ERROR で終わらせる
    assert response.status_code == 200
    assert response.json()["status"] == "ERROR"


# --- 入力の正規化 -------------------------------------------------------


def test_files_shape_from_web_frontend():
    encoded = base64.b64encode("こんにちは".encode()).decode()
    inputs = normalize(
        {"files": [{"key": "attachments", "files": [{"filename": "a.txt", "content": encoded}]}]}
    )
    assert inputs.files["attachments"][0].filename == "a.txt"
    assert inputs.files["attachments"][0].text() == "こんにちは"


def test_files_shape_from_async_docs():
    encoded = base64.b64encode(b"hello world").decode()
    inputs = normalize(
        {"files": [{"key": "source", "contents": encoded, "filename": "input.txt"}]}
    )
    assert inputs.files["source"][0].text() == "hello world"


def test_conversation_histories_plural_key_is_accepted():
    inputs = normalize(
        {
            "conversation_histories": [
                {"input": '{"question": "前回の質問"}', "output": "前回の回答", "createdDate": "1"}
            ]
        }
    )
    assert len(inputs.history) == 1
    assert inputs.history[0].input_obj["question"] == "前回の質問"


def test_conversation_history_singular_key_is_accepted():
    inputs = normalize({"conversation_history": [{"input": "a", "output": "b"}]})
    assert len(inputs.history) == 1


def test_checkbox_comma_separated_values():
    inputs = normalize({"options": "with_evidence,polite"})
    assert inputs.checkbox("options") == ["with_evidence", "polite"]
    assert normalize({"options": "polite"}).checkbox("options") == ["polite"]
    assert normalize({}).checkbox("options") == []


# --- URL 制約（源内側の SSRF 対策）--------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/invoke",  # HTTPS 必須
        "https://localhost/invoke",  # localhost 不可
        "https://127.0.0.1/invoke",  # ループバック不可
        "https://10.0.0.5/invoke",  # プライベート IP 不可
        "https://user:pass@example.com/invoke",  # 認証情報不可
        "https://example.com/invoke#frag",  # フラグメント不可
    ],
)
def test_endpoint_urls_rejected_by_genai(url):
    assert conformance.check_endpoint_url(url), f"{url} は拒否されるはず"


def test_valid_endpoint_url_passes():
    assert conformance.check_endpoint_url("https://api.example.go.jp/v1/invoke") == []


@pytest.mark.parametrize(
    "status_url",
    [
        "https://api.example.go.jp/status/1",  # 絶対 URL 不可
        "//evil.example.com/status/1",  # プロトコル相対不可
        "status/1",  # / 始まりでない
        "/status\\1",  # バックスラッシュ不可
    ],
)
def test_status_urls_rejected_by_genai(status_url):
    assert conformance.check_status_url(status_url), f"{status_url} は拒否されるはず"


# --- 応答チェッカー自体の検証 -------------------------------------------


def test_checker_flags_absolute_status_url():
    mode, problems = conformance.check_invoke_response(
        202, {"request_id": "a", "status": "PENDING", "status_url": "https://x.example.com/status/a"}
    )
    assert mode == "async"
    assert any("相対パス" in p for p in problems)


def test_checker_flags_slow_sync_response():
    mode, problems = conformance.check_invoke_response(200, {"outputs": "ok"}, elapsed_sec=45.0)
    assert any("タイムアウト" in p for p in problems)


def test_checker_flags_redirect():
    mode, problems = conformance.check_invoke_response(302, {})
    assert mode == "error"
    assert problems


def test_checker_flags_unknown_status_value():
    status, problems = conformance.check_status_response(200, {"status": "RUNNING"})
    assert any("想定外" in p for p in problems)
