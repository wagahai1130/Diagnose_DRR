# genai-exapp を Tailscale Funnel の固定 URL で公開するための起動スクリプト。
#
#   公開 URL : https://wagahai.tailcae03c.ts.net
#   Funnel   : 443 -> 127.0.0.1:8010（tailscaled が常駐、設定は永続）
#
# このファイルは UTF-8 BOM 付きで保存すること。Windows PowerShell 5.1 は
# BOM が無いと ANSI として読み、日本語コメントが壊れて行が欠落する。

# レポート本文に埋め込む地図画像の URL に使う。実際の公開 URL と食い違うと
# 画像だけが表示されなくなる。
$env:EXAPP_PUBLIC_URL = "https://wagahai.tailcae03c.ts.net"

Set-Location $PSScriptRoot

# 源内の「AI アプリ」設定に登録するキー。一致しないと /invoke は 401 を返す。
# 実体は .api-key.txt（Git 管理外）。変更したら再起動する。
$env:EXAPP_API_KEY = (Get-Content .api-key.txt -Raw).Trim()

.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
