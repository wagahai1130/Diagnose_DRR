"""LLM 連携のテスト。

実際の LLM は呼ばない（Ollama の起動も API キーも不要）。
HTTP と SDK を差し替えて、失敗時に必ずテンプレート文へ戻ることを確認する。
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import pytest

from app import hazard, llm, narrative
from app.diagnose import Diagnosis
from app.gsi import Elevation, GeoPoint
from app.hazard import FLOOD_PALETTE, HazardResult

FACTS = {
    "場所": "徳島県徳島市南常三島町二丁目１番地",
    "標高": 1.4,
    "該当する想定区域": [{"種別": "津波浸水想定", "程度": "1〜3m"}],
    "想定浸水深": "1〜3m",
    "安全な階": 2,
    "居住階": 1,
}


# --- 共通のフィクスチャ --------------------------------------------------


@pytest.fixture
def use_ollama(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "MODEL", "gemma3:4b")
    monkeypatch.delenv("EXAPP_USE_LLM", raising=False)


@pytest.fixture
def use_anthropic(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy-for-tests")
    monkeypatch.delenv("EXAPP_USE_LLM", raising=False)


def _ollama_response(content: str, status_code: int = 200):
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: {"message": {"role": "assistant", "content": content}},
        text=content,
    )


def _install_fake_ollama(monkeypatch, handler):
    monkeypatch.setattr(llm.httpx, "post", handler)


def _fake_message(text: str = "説明文です。", stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
    )


def _install_fake_anthropic(monkeypatch, beta_create, plain_create=None):
    def factory(*args, **kwargs):
        return SimpleNamespace(
            beta=SimpleNamespace(messages=SimpleNamespace(create=beta_create)),
            messages=SimpleNamespace(create=plain_create or beta_create),
        )

    monkeypatch.setattr(anthropic, "Anthropic", factory)


# --- 設定 ---------------------------------------------------------------


def test_ollama_is_the_default_provider():
    assert llm.DEFAULT_MODELS["ollama"] == "qwen3:8b"  # 実測で決定
    assert llm.DEFAULT_MODELS["anthropic"] == "claude-sonnet-4-6"


def test_ollama_needs_no_api_key(monkeypatch, use_ollama):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.available() is True


def test_anthropic_needs_an_api_key(monkeypatch, use_anthropic):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.available() is False


def test_llm_can_be_turned_off_explicitly(monkeypatch, use_ollama):
    monkeypatch.setenv("EXAPP_USE_LLM", "0")
    assert llm.available() is False
    assert llm.summarize(FACTS) is None


def test_provider_none_disables_everything(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "none")
    assert llm.available() is False


# --- プロンプト ---------------------------------------------------------


def test_prompt_contains_the_facts():
    prompt = llm.build_prompt(FACTS)
    assert "徳島県徳島市" in prompt
    assert "1〜3m" in prompt
    assert "事実だけ" in prompt


def test_system_prompt_forbids_declaring_safety():
    assert "安全です" in llm.SYSTEM_PROMPT
    assert "自治体" in llm.SYSTEM_PROMPT
    assert "単位を変えない" in llm.SYSTEM_PROMPT


# --- 生成文の検証（ローカルモデル対策の要）------------------------------


def test_verify_accepts_text_that_matches_the_facts():
    text = "この地点は津波浸水想定区域に該当し、想定される深さは1〜3mである。2階以上へ避難すること。"
    assert llm.verify(text, FACTS) == []


def test_verify_rejects_invented_depth():
    # 事実は 1〜3m。5m は生成側の作り話
    problems = llm.verify("想定される浸水は5mです。", FACTS)
    assert any("事実に無い数値" in p for p in problems)


def test_verify_rejects_unit_confusion():
    # 「1〜3m」を「30m」と取り違えるような誤りを弾く
    problems = llm.verify("浸水の深さは30mに達します。", FACTS)
    assert problems


@pytest.mark.parametrize("phrase", ["安全です", "心配ありません", "大丈夫です", "問題はありません"])
def test_verify_rejects_safety_declarations(phrase):
    problems = llm.verify(f"この地点は{phrase}。", FACTS)
    assert any("安全を宣言" in p for p in problems)


# 実測で gemma3:4b が出した誤りをそのまま回帰テストにする


def test_verify_rejects_inverted_affected_claim():
    """区域に該当しているのに『該当しません』と書く誤り（実際に観測）。"""
    text = "この地点そのものは、想定浸水区域に該当しませんが、周辺には区域があります。"
    problems = llm.verify(text, FACTS)
    assert any("該当しない" in p for p in problems)


@pytest.mark.parametrize(
    "sentence",
    [
        "しかしながら、周辺には想定区域が存在します。",
        "ただし、周辺に想定区域があるため、避難経路の確保に注意してください。",  # 実測で漏れた形
        "周辺には浸水想定区域が広がっています。",
        "周辺に想定区域が含まれます。",
    ],
)
def test_verify_rejects_fabricated_nearby_zone(sentence):
    """周辺に区域が無いのに『周辺にある』と書く誤り（活用形ちがいを網羅）。"""
    no_hazard = {"場所": "山間部", "標高": 912.4, "該当する想定区域": [], "周辺のみ該当": []}
    problems = llm.verify(f"この地域はどの想定区域にも該当しません。{sentence}", no_hazard)
    assert any("周辺" in p for p in problems), sentence


def test_verify_allows_correct_statement_that_nothing_is_nearby():
    """『周辺には区域がありません』は正しいので通す（否定の取りこぼし防止）。"""
    facts = {"場所": "内陸", "該当する想定区域": [{"種別": "洪水", "程度": "0.5〜3m"}], "周辺のみ該当": []}
    text = "この地点は洪水浸水想定区域に該当する。周辺地域に想定区域はないが、避難経路は確認すること。"
    assert llm.verify(text, facts) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "また、周辺地域の状況については情報がありませんので、ご注意ください。",  # 実測で観測
        "想定浸水深はデータにありません。",
        "垂直避難の可否については確認できません。",
    ],
)
def test_verify_rejects_no_information_claims(sentence):
    """調べた上での『該当なし』を『情報がない』と書くのは事実に反する。"""
    no_hazard = {"場所": "山間部", "標高": 912.4, "該当する想定区域": []}
    problems = llm.verify(f"この地点は想定区域に含まれていません。{sentence}", no_hazard)
    assert any("情報がない" in p for p in problems), sentence


@pytest.mark.parametrize(
    "text",
    [
        "この地点は津波浸水想定区域に該当します。",
        "2階以上へ避難してください。",
        "浸水の深さは1〜3mでしょう。",
    ],
)
def test_verify_rejects_polite_style(text):
    """防災文書は常体で統一する。敬体が混ざったら採用しない。"""
    assert any("常体" in p for p in llm.verify(text, FACTS)), text


def test_verify_accepts_plain_style():
    text = "この地点は津波浸水想定区域に該当する。想定される深さは1〜3mである。2階以上へ避難すること。"
    assert llm.verify(text, FACTS) == []


def test_verify_rejects_schema_key_leakage_in_other_forms():
    """『安全な階の情報』のような言い回しも内部項目名の露出として弾く。"""
    text = "想定浸水深や安全な階については、データには記載されていません。"
    assert any("内部の項目名" in p for p in llm.verify(text, FACTS))


def test_verify_rejects_schema_key_leakage():
    """内部のキー名をそのまま住民向け文章に出す誤り（実際に観測）。"""
    text = "安全な階については、現時点では情報がありません。世帯条件については情報がありません。"
    problems = llm.verify(text, FACTS)
    assert any("内部の項目名" in p for p in problems)


def test_verify_allows_correct_no_hazard_text():
    """該当が無い地点で『該当しません』と書くのは正しいので通す。"""
    no_hazard = {"場所": "山間部", "標高": 912.4, "該当する想定区域": [], "周辺のみ該当": []}
    text = (
        "この地点は、調べた想定区域のいずれにも該当しない。"
        "ただし想定を超える災害や内水氾濫は判定に含まれていないため、"
        "自治体の避難情報を確認すること。"
    )
    assert llm.verify(text, no_hazard) == []


def test_verify_allows_correct_nearby_warning():
    """周辺に区域が実際にある場合は、その記述を通す。"""
    facts = {
        "場所": "沿岸部",
        "該当する想定区域": [],
        "周辺のみ該当": [{"種別": "津波浸水想定", "程度": "1〜3m", "距離m": 46.0}],
    }
    text = "地点は区域外であるが、周辺に津波浸水想定の区域がある。避難経路を確認すること。"
    assert llm.verify(text, facts) == []


def test_clean_strips_thinking_tags():
    assert llm.clean("<think>どう書こうか</think>\n本文です。") == "本文です。"
    assert llm.clean("本文だけ") == "本文だけ"


# --- Ollama 経路 --------------------------------------------------------


def test_ollama_returns_generated_text(monkeypatch, use_ollama):
    text = "この地点は津波浸水想定区域に該当する。想定される深さは1〜3mである。避難情報に従うこと。"
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response(text))
    assert llm.summarize(FACTS) == text


def test_ollama_request_shape(monkeypatch, use_ollama):
    captured = {}

    def post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _ollama_response("本文である。")

    _install_fake_ollama(monkeypatch, post)
    llm.summarize(FACTS)

    assert captured["url"].endswith("/api/chat")
    payload = captured["json"]
    assert payload["model"] == "gemma3:4b"
    assert payload["stream"] is False
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"] == llm.SYSTEM_PROMPT
    # 事実の言い換えなので、ばらつきは抑える
    assert payload["options"]["temperature"] <= 0.3


def test_ollama_strips_thinking_output(monkeypatch, use_ollama):
    raw = "<think>まず整理する</think>\nこの地点の想定浸水深は1〜3mである。"
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response(raw))
    result = llm.summarize(FACTS)
    assert result is not None and "<think>" not in result


def test_ollama_not_running_falls_back(monkeypatch, use_ollama):
    def boom(*args, **kwargs):
        raise llm.httpx.ConnectError("connection refused")

    _install_fake_ollama(monkeypatch, boom)
    assert llm.summarize(FACTS) is None


def test_ollama_model_not_pulled_falls_back(monkeypatch, use_ollama):
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response("not found", status_code=404))
    assert llm.summarize(FACTS) is None


def test_ollama_empty_response_falls_back(monkeypatch, use_ollama):
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response(""))
    assert llm.summarize(FACTS) is None


def test_ollama_overlong_response_falls_back(monkeypatch, use_ollama):
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response("あ" * (llm.MAX_CHARS + 1)))
    assert llm.summarize(FACTS) is None


def test_ollama_hallucinated_depth_falls_back(monkeypatch, use_ollama):
    """小さいモデルが数値を取り違えても、そのまま住民には出さない。"""
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response("浸水の深さは9mです。"))
    assert llm.summarize(FACTS) is None


def test_ollama_safety_declaration_falls_back(monkeypatch, use_ollama):
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response("この地点は安全です。"))
    assert llm.summarize(FACTS) is None


# --- Anthropic 経路 -----------------------------------------------------


def test_anthropic_returns_generated_text(monkeypatch, use_anthropic):
    _install_fake_anthropic(monkeypatch, lambda **kw: _fake_message("この地点は…"))
    assert llm.summarize(FACTS) == "この地点は…"


def test_sonnet_does_not_send_fallbacks(monkeypatch, use_anthropic):
    """fallbacks は Opus 5 / Fable 5 系専用。Sonnet に送ると 400 になる。"""
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return _fake_message()

    _install_fake_anthropic(monkeypatch, create)
    llm.summarize(FACTS)

    assert "fallbacks" not in captured
    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["output_config"] == {"effort": "low"}


def test_opus5_sends_refusal_fallbacks(monkeypatch, use_anthropic):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return _fake_message()

    monkeypatch.setattr(llm, "MODEL", "claude-opus-5")
    _install_fake_anthropic(monkeypatch, create)
    llm.summarize(FACTS)

    assert captured["fallbacks"] == "default"


def test_anthropic_refusal_falls_back(monkeypatch, use_anthropic):
    _install_fake_anthropic(monkeypatch, lambda **kw: _fake_message("…", stop_reason="refusal"))
    assert llm.summarize(FACTS) is None


def test_anthropic_truncated_response_falls_back(monkeypatch, use_anthropic):
    _install_fake_anthropic(monkeypatch, lambda **kw: _fake_message("途中", stop_reason="max_tokens"))
    assert llm.summarize(FACTS) is None


def test_anthropic_api_error_falls_back(monkeypatch, use_anthropic):
    def boom(**kwargs):
        raise anthropic.APIConnectionError(request=None)

    _install_fake_anthropic(monkeypatch, boom)
    assert llm.summarize(FACTS) is None


def test_unsupported_parameter_falls_back_to_minimal_call(monkeypatch, use_anthropic):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "output_config" in kwargs:
            raise TypeError("unexpected keyword argument 'output_config'")
        return _fake_message("最小構成の応答")

    _install_fake_anthropic(monkeypatch, create)
    assert llm.summarize(FACTS) == "最小構成の応答"
    assert len(calls) == 2
    assert calls[1]["system"] == llm.SYSTEM_PROMPT


# --- LLM に渡す事実の作り方 ---------------------------------------------
#
# 空の項目を渡すとモデルがそれを埋めようとして作り話を始める（実測で確認）。
# 渡さなければ書きようがない、というのがこの一連のテストの主旨。


def _plain_diagnosis(**kwargs) -> Diagnosis:
    base = dict(
        query="テスト",
        point=GeoPoint(lat=34.0, lon=134.0, title="テスト市"),
        elevation=Elevation(value=12.0, source="1m（レーザ）"),
        hazards=[],
    )
    base.update(kwargs)
    return Diagnosis(**base)


def test_facts_omit_empty_fields():
    facts = narrative.facts_for_llm(_plain_diagnosis())
    for key in ("周辺のみ該当", "想定浸水深", "安全な階", "居住階", "世帯条件"):
        assert key not in facts, f"{key} が空のまま渡されています"


def test_facts_always_include_the_headline_finding():
    """該当区域は、空でも渡す（診断の主結論なので）。"""
    facts = narrative.facts_for_llm(_plain_diagnosis())
    assert facts["該当する想定区域"] == []
    assert "場所" in facts


def test_facts_include_fields_that_have_values():
    depth = FLOOD_PALETTE[(255, 216, 192)]
    facts = narrative.facts_for_llm(
        _plain_diagnosis(
            hazards=[HazardResult(layer=hazard.LAYERS_BY_KEY["flood"], at_point=depth)],
            floor=1,
            conditions=["elderly"],
        )
    )
    assert facts["想定浸水深"] == "0.5〜3m"
    assert facts["安全な階"] == 3  # 上限 3m で評価（3 // 3 + 2）
    assert facts["居住階"] == 1
    assert facts["世帯条件"] == ["高齢者・要介護者がいる"]


def test_facts_omit_missing_elevation():
    facts = narrative.facts_for_llm(_plain_diagnosis(elevation=Elevation(value=None)))
    assert "標高" not in facts


def test_facts_include_nearby_only_when_present():
    depth = FLOOD_PALETTE[(255, 216, 192)]
    facts = narrative.facts_for_llm(
        _plain_diagnosis(
            hazards=[
                HazardResult(
                    layer=hazard.LAYERS_BY_KEY["tsunami"],
                    nearby=depth,
                    nearby_distance_m=46.0,
                )
            ]
        )
    )
    assert facts["周辺のみ該当"][0]["距離m"] == 46.0


# --- レポートへの組み込み -----------------------------------------------


def _diagnosis() -> Diagnosis:
    depth = FLOOD_PALETTE[(255, 216, 192)]  # 0.5〜3m
    return Diagnosis(
        query="テスト住所",
        point=GeoPoint(lat=34.0, lon=134.0, title="徳島県テスト市"),
        elevation=Elevation(value=1.4, source="1m（レーザ）"),
        hazards=[HazardResult(layer=hazard.LAYERS_BY_KEY["flood"], at_point=depth)],
    )


def test_report_uses_template_when_llm_is_off(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "none")
    body, engine = narrative.summarize(_diagnosis())
    assert engine == "template"
    assert "想定される浸水の深さ" in body
    assert "Claude" not in narrative.render_report(_diagnosis())


def test_report_notes_which_model_wrote_the_text(monkeypatch, use_ollama):
    text = "この地点は洪水浸水想定区域に該当し、想定される深さは0.5〜3mである。避難情報に従うこと。"
    _install_fake_ollama(monkeypatch, lambda *a, **kw: _ollama_response(text))
    report = narrative.render_report(_diagnosis())

    assert text in report
    assert "gemma3:4b" in report  # 誰が書いたかを明示する
    # 事実の表と免責はモデルに書かせず、テンプレート側で必ず出す
    assert "自治体が発表する避難情報に従うこと" in report
    assert "国土地理院" in report
    assert "洪水浸水想定区域（想定最大規模）" in report


def test_llm_failure_still_produces_a_full_report(monkeypatch, use_ollama):
    def boom(*args, **kwargs):
        raise llm.httpx.ConnectError("connection refused")

    _install_fake_ollama(monkeypatch, boom)
    report = narrative.render_report(_diagnosis())

    assert "想定される浸水の深さ" in report  # テンプレート文
    assert "自治体が発表する避難情報に従うこと" in report
