"""源内 Web に「行政実務用 AI アプリ」として登録される REST API。

源内 Web からのリクエスト:
    POST <このアプリのエンドポイント>
    Content-Type: application/json
    x-api-key:  源内が Secrets Manager で管理しているキー
    x-user-id:  Cognito sub から生成された安定ユーザー ID
    {"inputs": {...}, "sessionId": "<uuid-v4>"}

応答は 2 通り:
    同期  : 200 {"outputs": "Markdown 文字列"}
    非同期: 202 {"outputs": ..., "request_id": ..., "status": "PENDING",
                 "status_url": "/status/<request_id>"}
            → 源内が status_url を GET しに来る（同一オリジンの相対パス必須）

同期の既定タイムアウトは 29 秒。LLM を呼ぶなら非同期にすること。
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import jobs, logic
from .normalize import normalize
from .schema import APP_DESCRIPTION, APP_NAME, REQUEST_SCHEMA

API_KEY = os.environ.get("EXAPP_API_KEY", "dev-local-key")
REQUIRE_API_KEY = os.environ.get("EXAPP_REQUIRE_API_KEY", "1") != "0"
DEFAULT_MODE = os.environ.get("EXAPP_MODE", "async")  # "async" | "sync"

app = FastAPI(title=f"{APP_NAME}（源内 AI アプリ）", description=APP_DESCRIPTION, docs_url="/docs")
api = APIRouter()


def _unauthorized() -> JSONResponse:
    # 源内側は本文の outputs をそのまま画面に出すので、原因が分かる文言にしておく
    return JSONResponse(status_code=401, content={"outputs": "API キーが不正です。"})


def _check_api_key(provided: str | None) -> bool:
    if not REQUIRE_API_KEY:
        return True
    return bool(provided) and hmac.compare_digest(provided, API_KEY)


@api.post("/invoke")
async def invoke(
    request: Request,
    x_api_key: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
):
    if not _check_api_key(x_api_key):
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 壊れた JSON も源内へ 400 で返す
        return JSONResponse(status_code=400, content={"outputs": "JSON の形式が不正です。"})

    if not isinstance(body, dict) or not isinstance(body.get("inputs"), dict):
        return JSONResponse(
            status_code=400, content={"outputs": "inputs オブジェクトが必要です。"}
        )

    inputs = normalize(body["inputs"])
    user_id = x_user_id or ""
    mode = request.query_params.get("mode", DEFAULT_MODE)

    if mode == "sync":
        try:
            result = logic.run(inputs, user_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"outputs": f"処理中にエラーが発生しました: {exc}"},
            )
        # 同期応答では artifacts を返せないので Base64 を本文に埋めるか、非同期にする
        return {"outputs": result.outputs}

    job = jobs.store.create()
    # 受付応答は必ず PENDING。job.status を読むと、処理が即座に終わった場合に
    # COMPLETED が載ってしまい、源内側の 202 応答チェックに合わなくなる
    accepted = JSONResponse(
        status_code=202,
        content={
            "outputs": "リクエストを受け付けました",
            "request_id": job.request_id,
            "status": jobs.PENDING,
            # 絶対 URL にすると源内側で拒否される
            "status_url": f"/status/{job.request_id}",
        },
    )
    jobs.store.start(job, inputs, user_id)
    return accepted


@api.get("/status/{request_id}")
async def status(request_id: str, x_api_key: str | None = Header(default=None)):
    if not _check_api_key(x_api_key):
        return _unauthorized()

    job = jobs.store.get(request_id)
    if job is None:
        # 404 を返すと源内は再試行し続けるので、ERROR として終わらせる
        return JSONResponse(
            status_code=200,
            content={
                "request_id": request_id,
                "status": jobs.ERROR,
                "progress": "",
                "error": {
                    "message": "指定されたリクエストが見つかりません。",
                    "details": "Unknown request_id",
                },
            },
        )
    return job.to_status_response()


@api.get("/schema")
async def schema():
    """源内のチーム管理画面「リクエスト形式」に貼る JSON。"""
    return REQUEST_SCHEMA


@api.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(api)

# 診断画面。これが利用者の入口なので本番でも有効にしておく（EXAPP_UI=0 で API だけにできる）
if os.environ.get("EXAPP_UI", "1") != "0":
    from mock.router import router as mock_router

    # /ui が本来の画面。/mock は以前の名前で、当面は互換のため残す
    app.include_router(mock_router, prefix="/ui")
    app.include_router(mock_router, prefix="/mock", include_in_schema=False)
