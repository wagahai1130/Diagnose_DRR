# 源内 AI アプリ (Python / FastAPI)

デジタル庁の生成 AI 利活用基盤 **源内 (genai-web)** に「行政実務用 AI アプリ」として
登録される外部マイクロサービスの雛形。あわせて、源内 Web の代役を務める
**ローカルモック**を同梱しているので、AWS 環境なしで開発できる。

```
ブラウザ ──▶ 源内 Web (React / AWS)  ──▶ このアプリ (FastAPI)
                  ↑ 本番                        ↑ ここだけ書く
ブラウザ ──▶ /mock (同梱のモック)     ──▶ このアプリ (FastAPI)
                  ↑ ローカル開発
```

源内 Web 側がログイン・チーム権限・入力フォーム UI・実行履歴・ファイル保管・
非同期ポーリングを持ってくれるので、こちらは REST API を 1 本書くだけでよい。

## 使い方

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8010
```

- モック画面: http://127.0.0.1:8010/mock
- API ドキュメント: http://127.0.0.1:8010/docs
- 契約テスト: `.venv/Scripts/python.exe -m pytest -q`

## ディレクトリ

| ファイル | 役割 |
| --- | --- |
| `app/schema.py` | 源内の「リクエスト形式」に貼る JSON。ここを書くと入力フォームが自動生成される |
| `app/logic.py` | 実処理の組み立て（住所 → 診断 → レポート） |
| `app/diagnose.py` | 診断本体。標高・ハザードを集めて Diagnosis にまとめる |
| `app/gsi.py` | 国土地理院の公開 API（住所検索・標高・タイル） |
| `app/hazard.py` | ハザードタイルの色判定 |
| `app/narrative.py` | 説明文の生成（Claude API / テンプレートの切り替え） |
| `app/llm.py` | Claude API 呼び出し。キーが無ければ何もしない |
| `app/main.py` | 源内から叩かれる REST API (`POST /invoke`, `GET /status/{id}`) |
| `app/normalize.py` | 届いた inputs の正規化（ファイル・会話履歴の形の揺れを吸収） |
| `app/jobs.py` | 非同期ジョブ管理（インメモリ。本番は外部ストアに置き換える） |
| `app/conformance.py` | 源内の受け入れ条件チェッカー |
| `mock/` | 源内 Web の代役。フォーム自動生成・呼び出し・ポーリング・仕様チェック |
| `tests/test_contract.py` | 契約テスト。ここが緑なら仕様違反で落ちない |

## 文章生成に LLM を使う（任意）

説明文だけを LLM に書かせられる。**使わなくてもレポートは完成する**（テンプレート文）。

### Ollama（既定・無料・API キー不要）

1. https://ollama.com からインストール
2. モデルを取得（約 3GB）

```bash
ollama pull gemma3:4b
```

3. そのままアプリを起動すれば使われる（`ollama serve` は通常自動で常駐）

### Claude API を使う場合

```bash
export EXAPP_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

### モデルの選び方（実測で決めた）

既定は `qwen3:8b`。RTX 3060 Laptop（VRAM 6GB）+ RAM 32GB での実測結果:

| モデル | 浸水あり | 該当なし | 周辺のみ | 平均秒 |
| --- | --- | --- | --- | --- |
| `gemma3:4b` | 3/3 | 2/3 | 3/3 | 4〜8 |
| **`qwen3:8b`** | **3/3** | **2/3** | **3/3** | 9〜15 |

数字だけ見ると互角だが、**中身を読むと差がある**。gemma3:4b は該当なしの地点で
「周辺に想定区域があります」と存在しない区域を捏造し、内部の項目名をそのまま
住民向け文章に出した。qwen3:8b は 3 ケースとも事実関係が正確だった。

再測定はこれで行う（本番と同じ `facts_for_llm()` → 同じプロンプト → 同じ検証）:

```bash
python tools/compare_models.py gemma3:4b qwen3:8b --runs 3 --full
```

VRAM 6GB クラスの候補:

| モデル | サイズ | 備考 |
| --- | --- | --- |
| `qwen3:4b` | 2.5GB | 未検証。軽い |
| `gemma3:4b` | 3.3GB | **不採用**（該当なしで捏造） |
| `qwen3:8b` | 5.2GB | **既定**。VRAM にほぼ収まる |
| `qwen3:14b` | 9.3GB | 未検証。VRAM から溢れるが非同期なら許容範囲 |
| `glm-4.7-flash` | 30B 級 | 6GB VRAM には重い |
| `glm-5.2` | 756B | `:cloud` タグのみ。**ローカル実行不可** |

源内の AI アプリは非同期実行（最大 96 時間ポーリング）なので、生成に 1〜2 分かかっても
仕様上は問題ない。速度より検証通過率を優先してよい。

`qwen3` などの思考モデルには `think: false` を送って思考文を抑止している
（`EXAPP_LLM_THINK=1` で思考を有効化できる）。

### 検証で防げないもの

`llm.verify()` は数値の取り違え・該当関係の反転・安全宣言を機械的に弾くが、
**文章としての微妙な誤りは防げない**。実測で観測した例:

> 洪水は 0.5〜3m。津波浸水想定区域でも**同様の**浸水深が予想されています

事実は津波 1〜3m で「同様」ではない。書かれた数値自体は事実に存在するため検出できない。

**住民に直接出す用途では、生成文を人が読む前提にすること。** それが難しい場合は
`EXAPP_LLM_PROVIDER=none` でテンプレート文に固定するのが安全。

### 設定

| 環境変数 | 既定 | 説明 |
| --- | --- | --- |
| `EXAPP_LLM_PROVIDER` | `ollama` | `ollama` / `anthropic` / `none` |
| `EXAPP_LLM_MODEL` | `gemma3:4b` / `claude-sonnet-4-6` | プロバイダごとの既定 |
| `EXAPP_LLM_TIMEOUT` | `180` | 秒。ローカル生成は遅いので長め |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | |
| `EXAPP_USE_LLM` | `1` | `0` で強制的にテンプレートのみ |

### 安全側の作り

**LLM に判定はさせない。** 標高・浸水深・区域の判定は公開データの色判定で確定済みで、
モデルに渡すのは `narrative.facts_for_llm()` が作る事実の辞書だけ。

ローカルの小さいモデルは数値を取り違えることがあるので、生成文は `llm.verify()` で検査する。
以下に当たれば**採用せずテンプレート文に戻す**:

- 「安全です」「心配ありません」など、安全を宣言する表現
- 事実に無い浸水深（例: 想定が 1〜3m なのに「9m」と書く）
- 空・3000 字超・思考タグの残骸

Ollama が起動していない、モデル未取得、通信失敗、といった場合も同様にテンプレートへ戻る。
ハザードの表と免責事項は LLM を通さず、常にテンプレート側が出力する。
LLM が書いた場合は、レポート末尾にどのモデルが書いたかが明記される。

## 源内との契約

源内 Web が送ってくるもの:

```
POST <エンドポイント>
Content-Type: application/json
x-api-key: <源内が Secrets Manager で管理>
x-user-id: <Cognito sub から生成した安定 ID>

{"inputs": { ... }, "sessionId": "<uuid-v4>"}
```

返すもの:

| | 同期 | 非同期（推奨） |
| --- | --- | --- |
| 応答 | `200 {"outputs": "Markdown"}` | `202 {"outputs":…, "request_id":…, "status":"PENDING", "status_url":"/status/<id>"}` |
| その後 | なし | 源内が `status_url` を GET → `COMPLETED` で `outputs` と `artifacts` |

`EXAPP_MODE=sync|async`（既定 `async`）またはエンドポイント URL の `?mode=` で切り替わる。

## ハマりどころ

源内側の実装（`packages/cdk/lambda/`）を読んで確認した、ドキュメントに書かれていない/
紛らわしい制約。`app/conformance.py` がすべて自動チェックする。

1. **エンドポイントは HTTPS 必須。localhost とプライベート IP は登録できない**
   （`utils/exAppUrlSecurity.ts`）。ローカルの Python サーバーを源内から直接は叩けないので、
   本番接続時は Cloudflare Tunnel などで公開するか、クラウドにデプロイする。
2. **リダイレクト（3xx）は拒否される**（`redirect: 'manual'`）。
3. **同期のタイムアウトは既定 29 秒**（`stack-input.ts` の `exAppInvokeTimeoutSeconds`）。
   LLM を呼ぶなら非同期にする。
4. **`status_url` は同一オリジンの相対パス必須**。絶対 URL・`//` 始まり・`\` を含むものは拒否。
5. **会話履歴のキー名が入出力で違う**。登録 JSON に書くのは `conversation_history`（単数）、
   実際に届くのは `inputs.conversation_histories`（複数、`{input, output, createdDate}` の配列）。
   `input` は前回の inputs を JSON 文字列にしたもの。
6. **ファイルの形が 2 通りある**。同期系は `[{key, files:[{filename, content}]}]`、
   非同期系のドキュメントは `[{key, contents, filename}]`。`normalize.py` が両方を受ける。
7. **`max_size` は Base64 変換後のサイズで判定される**（実質 3/4 に目減りする）。
8. **`status` に想定外の値を返すとポーリングが止まらない**。
   `PENDING` / `IN_PROGRESS` / `COMPLETED` / `ERROR` 以外は未完了扱いで最大 96 時間再試行される。
   未知の `request_id` も 404 ではなく `status: ERROR` で返して終わらせること。
9. **`usageMetadata` は配列で返すとコスト集計に載る**（任意）。

## 本番の源内に登録するとき

1. HTTPS で公開されたホストにデプロイする（`https://<host>/invoke`）。
2. `EXAPP_MOCK=0` と `EXAPP_API_KEY=<源内に登録するキー>` を設定してモックを無効化する。
3. 源内 Web の「チーム管理 > AI アプリ登録」で、エンドポイントと
   `app/schema.py` の JSON（`GET /schema` でも取得できる）を登録する。

## 参考

- 源内 Web: https://github.com/digital-go-jp/genai-web （`docs/AIアプリAPI仕様.md`）
- AI アプリ実装例（Azure / GCP / AWS）: https://github.com/digital-go-jp/genai-ai-api
