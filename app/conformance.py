"""源内 Web の受け入れ条件チェッカー。

源内 Web 側の実装 (packages/cdk/lambda/invokeExApp.ts, pollExAppStatus.ts,
utils/exAppUrlSecurity.ts) から読み取った検証を、こちら側で先に実行する。
モック画面とテストの両方から使う。

戻り値は違反メッセージのリスト。空なら仕様準拠。
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
from urllib.parse import urlsplit

MAX_ENDPOINT_URL_LENGTH = 4096
MAX_STATUS_URL_LENGTH = 2048
DEFAULT_SYNC_TIMEOUT_SEC = 29  # cdk/lib/stack-input.ts の exAppInvokeTimeoutSeconds 既定値
VALID_STATUSES = {"PENDING", "IN_PROGRESS", "COMPLETED", "ERROR"}


def check_endpoint_url(url: str) -> list[str]:
    """源内に登録するエンドポイント URL が assertPublicEndpointUrl を通るか。"""
    problems: list[str] = []
    if not url or len(url) > MAX_ENDPOINT_URL_LENGTH:
        return ["エンドポイント URL が空か、4096 文字を超えています。"]
    if url.strip() != url:
        problems.append("エンドポイント URL の前後に空白があります。")

    parts = urlsplit(url)
    if parts.scheme != "https":
        problems.append(f"HTTPS が必須です (現在: {parts.scheme or '(なし)'})。")
    if not parts.hostname:
        problems.append("ホスト名がありません。")
    if parts.username or parts.password:
        problems.append("URL に認証情報 (user:pass@) は含められません。")
    if parts.fragment:
        problems.append("URL にフラグメント (#) は含められません。")

    host = (parts.hostname or "").rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        problems.append("localhost は登録できません。公開ホスト名が必要です。")
    else:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not addr.is_global:
                problems.append(f"プライベート/予約 IP は登録できません ({host})。")

    return problems


def check_status_url(status_url, endpoint_url: str = "") -> list[str]:
    """202 応答の status_url が resolveRelativeStatusUrl を通るか。"""
    problems: list[str] = []
    if not isinstance(status_url, str) or not status_url:
        return ["status_url が文字列ではありません。"]
    if len(status_url) > MAX_STATUS_URL_LENGTH:
        problems.append("status_url が 2048 文字を超えています。")
    if status_url.strip() != status_url:
        problems.append("status_url の前後に空白があります。")
    if not status_url.startswith("/"):
        problems.append(
            f"status_url は / で始まる相対パスにしてください (現在: {status_url[:60]})。"
            " 絶対 URL は拒否されます。"
        )
    if status_url.startswith("//"):
        problems.append("status_url が // で始まっています (プロトコル相対 URL は拒否されます)。")
    if "\\" in status_url:
        problems.append("status_url にバックスラッシュは含められません。")
    if "#" in status_url:
        problems.append("status_url にフラグメント (#) は含められません。")
    return problems


def check_invoke_response(
    http_status: int,
    body,
    elapsed_sec: float = 0.0,
    timeout_sec: int = DEFAULT_SYNC_TIMEOUT_SEC,
) -> tuple[str, list[str]]:
    """POST /invoke の応答を検証する。

    戻り値: (判定されたモード, 違反リスト)
      モードは "async" / "sync" / "error"
    """
    problems: list[str] = []

    if 300 <= http_status < 400:
        return "error", ["3xx リダイレクトは源内側で拒否されます (redirect: manual)。"]

    if not isinstance(body, dict):
        problems.append(
            "JSON オブジェクトを返してください。"
            " JSON でない応答は本文まるごとが outputs として扱われます。"
        )
        return "sync", problems

    status_url = body.get("status_url")

    if http_status == 202 and isinstance(status_url, str):
        problems += check_status_url(status_url)
        if not isinstance(body.get("request_id"), str) or not body.get("request_id"):
            problems.append("202 応答には request_id (文字列) を含めてください。")
        if body.get("status") not in {"PENDING", "IN_PROGRESS"}:
            problems.append('202 応答の status は "PENDING" か "IN_PROGRESS" にしてください。')
        return "async", problems

    if http_status == 202:
        problems.append(
            "202 を返しているのに status_url がありません。"
            " 源内側は同期応答として扱い、ポーリングを開始しません。"
        )
    if status_url is not None and http_status != 202:
        problems.append(
            f"status_url を返していますが HTTP ステータスが {http_status} です。"
            " 非同期にするには 202 を返してください。"
        )

    if http_status >= 400:
        return "error", problems

    if not isinstance(body.get("outputs"), str):
        problems.append("同期応答には outputs (文字列) が必要です。Markdown 可。")

    if elapsed_sec > timeout_sec:
        problems.append(
            f"応答に {elapsed_sec:.1f} 秒かかりました。"
            f" 同期実行の既定タイムアウトは {timeout_sec} 秒です。非同期実行にしてください。"
        )

    problems += _check_usage_metadata(body)
    return "sync", problems


def check_status_response(http_status: int, body) -> tuple[str, list[str]]:
    """GET status_url の応答を検証する。戻り値: (status, 違反リスト)"""
    problems: list[str] = []

    if http_status >= 400:
        return "", [f"ステータス確認が HTTP {http_status} を返しました。源内側は再試行し続けます。"]
    if not isinstance(body, dict):
        return "", ["ステータス応答が JSON オブジェクトではありません。"]

    status = body.get("status")
    if not isinstance(status, str):
        return "", ["ステータス応答に status (文字列) がありません。"]
    if status not in VALID_STATUSES:
        problems.append(
            f'status が想定外の値です ("{status}")。'
            " PENDING / IN_PROGRESS / COMPLETED / ERROR 以外は未完了扱いになり、"
            " ポーリングが止まりません。"
        )

    if status == "COMPLETED":
        if not isinstance(body.get("outputs"), str):
            problems.append("COMPLETED 時は outputs (文字列) を返してください。")
        problems += _check_artifacts(body.get("artifacts"))
        problems += _check_usage_metadata(body)

    if status == "ERROR" and not isinstance(body.get("error"), dict):
        problems.append("ERROR 時は error: {message, details} を返してください。")

    return status, problems


def _check_artifacts(artifacts) -> list[str]:
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        return ["artifacts は配列にしてください。"]

    problems: list[str] = []
    for index, item in enumerate(artifacts):
        label = f"artifacts[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{label} がオブジェクトではありません。")
            continue
        if not isinstance(item.get("display_name"), str) or not item.get("display_name"):
            problems.append(f"{label}.display_name (文字列) が必要です。")
        contents = item.get("contents")
        if not isinstance(contents, str):
            problems.append(f"{label}.contents (Base64 文字列) が必要です。")
            continue
        try:
            base64.b64decode(contents, validate=True)
        except (binascii.Error, ValueError):
            problems.append(f"{label}.contents が Base64 として復号できません。")
    return problems


def _check_usage_metadata(body: dict) -> list[str]:
    usage = body.get("usageMetadata")
    if usage is None:
        return []
    if not isinstance(usage, list):
        return ["usageMetadata は配列にしてください。配列でないとコスト集計に載りません。"]
    return []
