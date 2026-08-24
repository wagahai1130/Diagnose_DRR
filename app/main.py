"""診断 API。

  POST /invoke        診断の実行
  GET  /status/{id}   非同期実行の進捗と結果
  GET  /schema        入力項目の定義
  GET  /health        死活確認

リクエスト:
    POST /invoke
    Content-Type: application/json
    x-api-key:  発行したキー
    x-user-id:  利用者の識別子（任意）
    {"inputs": {...}, "sessionId": "<uuid-v4>"}

応答は 2 通り:
    同期  : 200 {"outputs": "Markdown 文字列"}
    非同期: 202 {"outputs": ..., "request_id": ..., "status": "PENDING",
                 "status_url": "/status/<request_id>"}
            → 呼び出し元が status_url を GET して完了を待つ

診断に 10〜20 秒かかるため既定は非同期。
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from . import jobs, logic, mapstore
from .normalize import normalize
from .schema import APP_DESCRIPTION, APP_NAME, REQUEST_SCHEMA

API_KEY = os.environ.get("EXAPP_API_KEY", "dev-local-key")
REQUIRE_API_KEY = os.environ.get("EXAPP_REQUIRE_API_KEY", "1") != "0"
DEFAULT_MODE = os.environ.get("EXAPP_MODE", "async")  # "async" | "sync"

app = FastAPI(title=APP_NAME, description=APP_DESCRIPTION, docs_url="/docs")
api = APIRouter()


def _unauthorized() -> JSONResponse:
    # outputs はそのまま画面に出るので、原因が分かる文言にしておく
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
    except Exception:  # noqa: BLE001 - 壊れた JSON は 400 で返す
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
    accepted = JSONResponse(
        status_code=202,
        content={
            "outputs": "リクエストを受け付けました",
            "request_id": job.request_id,
            "status": jobs.PENDING,
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


@api.get("/maps/{map_id}.png")
async def map_image(map_id: str):
    """生成した地図の配信。レポート本文から画像として参照される。

    認証は掛けない。ID は推測困難な 32 桁で、内容は公開データの地図であるため。
    """
    png = mapstore.store.get(map_id)
    if png is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@api.get("/schema")
async def schema():
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
