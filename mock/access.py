"""診断画面のアクセス制限。

一時 URL（Cloudflare Tunnel など）で外部に公開するときに使う。
キーを知っている人だけが画面と診断を使えるようにする。

  EXAPP_ACCESS_KEY を設定 → 有効。URL に ?key=... が必要
  未設定                  → 無効（ローカル開発ではそのまま使える）

強固な認証ではない。「URL が転送されても勝手に使われない」程度の目的。
不特定多数へ公開する場合は、この前段に本物の認証を置くこと。
"""

from __future__ import annotations

import hmac
import os

COOKIE_NAME = "exapp_access"
QUERY_NAME = "key"


def required_key() -> str:
    return os.environ.get("EXAPP_ACCESS_KEY", "")


def enabled() -> bool:
    return bool(required_key())


def is_allowed(provided: str | None) -> bool:
    """キーが一致するか。制限が無効なら常に許可。"""
    if not enabled():
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, required_key())
