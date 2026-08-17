"""源内 Web の代役。ローカル開発用。

本物の源内 Web がやっていることを再現する:
  1. リクエスト形式 JSON から入力フォームを自動生成する
  2. x-api-key / x-user-id を付けて {"inputs": ...} を POST する
  3. 202 + status_url が返ったら status_url をポーリングする
  4. COMPLETED の outputs を Markdown として表示し、artifacts をダウンロードさせる

加えて、本物にはない機能として応答の仕様違反を検出して表示する
(app/conformance.py)。ここを緑にしてから本物の源内に登録すれば手戻りがない。

本番デプロイでは EXAPP_MOCK=0 を設定してこのルーターを無効化すること。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import conformance
from app.schema import REQUEST_SCHEMA

from . import access

router = APIRouter()

INDEX_HTML = Path(__file__).with_name("index.html")

# 本物の源内は 30 秒間隔でポーリングするが、開発中は待てないので短くする
POLL_INTERVAL_SEC = float(os.environ.get("MOCK_POLL_INTERVAL", "0.7"))
POLL_TIMEOUT_SEC = float(os.environ.get("MOCK_POLL_TIMEOUT", "120"))


DENIED_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>災害リスク診断</title></head>
<body style="font-family:system-ui;max-width:520px;margin:80px auto;padding:0 20px;line-height:1.8">
<h1 style="font-size:18px">アクセスキーが必要である</h1>
<p>この画面は限定公開である。共有された URL を、末尾の <code>?key=…</code> まで含めて開くこと。</p>
</body></html>"""


def _access_key(request: Request) -> str | None:
    return request.query_params.get(access.QUERY_NAME) or request.cookies.get(access.COOKIE_NAME)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    key = _access_key(request)
    if not access.is_allowed(key):
        return HTMLResponse(DENIED_HTML, status_code=401)

    html = INDEX_HTML.read_text(encoding="utf-8")
    html = html.replace("__SCHEMA_JSON__", json.dumps(REQUEST_SCHEMA, ensure_ascii=False))
    response = HTMLResponse(html)
    if access.enabled() and key:
        # 以後の /run 呼び出しでキーを持ち回れるようにする
        response.set_cookie(access.COOKIE_NAME, key, httponly=True, samesite="strict")
    return response


def _resolve_status_url(status_url: str, endpoint: str) -> str:
    """源内と同じく、status_url は同一オリジンの相対パスとして解決する。"""
    parts = urlsplit(endpoint)
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")) + status_url


@router.post("/run")
async def run(request: Request) -> dict:
    if not access.is_allowed(_access_key(request)):
        return JSONResponse(
            status_code=401, content={"ok": False, "violations": ["アクセスキーが不正である。"]}
        )

    payload = await request.json()
    inputs = payload.get("inputs") or {}
    endpoint = payload.get("endpoint") or (str(request.base_url).rstrip("/") + "/invoke")
    api_key = payload.get("apiKey") or os.environ.get("EXAPP_API_KEY", "dev-local-key")
    user_id = payload.get("userId") or "mock-stable-user-id"
    session_id = payload.get("sessionId") or str(uuid.uuid4())

    timeline: list[dict] = []
    violations: list[str] = []
    started = time.monotonic()

    def log(label: str, detail: str = "") -> None:
        timeline.append({"at": round(time.monotonic() - started, 2), "label": label, "detail": detail})

    # 本番登録時に効いてくる URL 制約は警告として出す（ローカルの http は当然引っかかる）
    deploy_warnings = conformance.check_endpoint_url(endpoint)

    body = {"inputs": inputs, "sessionId": session_id}
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "x-user-id": user_id,
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        log("POST", endpoint)
        try:
            response = await client.post(endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            log("接続失敗", str(exc))
            return {
                "ok": False,
                "timeline": timeline,
                "violations": [f"エンドポイントに接続できません: {exc}"],
                "deployWarnings": deploy_warnings,
            }

        elapsed = time.monotonic() - started
        try:
            response_body = response.json()
        except ValueError:
            response_body = response.text

        log(f"HTTP {response.status_code}", f"{elapsed:.2f} 秒")

        mode, problems = conformance.check_invoke_response(
            response.status_code, response_body, elapsed_sec=elapsed
        )
        violations += problems

        if mode != "async":
            return {
                "ok": response.status_code < 400 and not violations,
                "mode": mode,
                "status": "COMPLETED" if response.status_code < 400 else "ERROR",
                "result": response_body,
                "timeline": timeline,
                "violations": violations,
                "deployWarnings": deploy_warnings,
                "sessionId": session_id,
            }

        # --- ここから非同期のポーリング ---
        status_url = response_body.get("status_url")
        if conformance.check_status_url(status_url):
            return {
                "ok": False,
                "mode": "async",
                "status": "ERROR",
                "result": response_body,
                "timeline": timeline,
                "violations": violations,
                "deployWarnings": deploy_warnings,
                "sessionId": session_id,
            }

        polling_url = _resolve_status_url(status_url, endpoint)
        log("ポーリング開始", polling_url)

        last_progress = ""
        status_body: dict | str = {}
        deadline = time.monotonic() + POLL_TIMEOUT_SEC

        while True:
            if time.monotonic() > deadline:
                violations.append(
                    f"{POLL_TIMEOUT_SEC:.0f} 秒待っても終了状態になりませんでした。"
                    " 本物の源内は最大 96 時間待ちますが、開発中は打ち切ります。"
                )
                log("タイムアウト")
                break

            try:
                status_response = await client.get(
                    polling_url, headers={"x-api-key": api_key, "x-user-id": user_id}
                )
            except httpx.HTTPError as exc:
                violations.append(f"ステータス確認に失敗しました: {exc}")
                log("ポーリング失敗", str(exc))
                break

            try:
                status_body = status_response.json()
            except ValueError:
                status_body = status_response.text

            job_status, problems = conformance.check_status_response(
                status_response.status_code, status_body
            )
            for problem in problems:
                if problem not in violations:
                    violations.append(problem)

            if isinstance(status_body, dict):
                progress = str(status_body.get("progress") or "")
                if progress and progress != last_progress:
                    log(f"{job_status or '?'}", progress)
                    last_progress = progress

            if job_status in ("COMPLETED", "ERROR"):
                log("終了", job_status)
                break
            if not job_status:
                break

            await asyncio.sleep(POLL_INTERVAL_SEC)

        final_status = status_body.get("status") if isinstance(status_body, dict) else "ERROR"
        return {
            "ok": final_status == "COMPLETED" and not violations,
            "mode": "async",
            "status": final_status,
            "result": status_body,
            "timeline": timeline,
            "violations": violations,
            "deployWarnings": deploy_warnings,
            "sessionId": session_id,
        }
