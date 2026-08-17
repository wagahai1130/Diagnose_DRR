import pytest


@pytest.fixture(autouse=True)
def disable_llm_by_default(monkeypatch):
    """テストが実環境の API キーを拾って課金しないようにする。

    LLM 経路を試すテストは with_api_key フィクスチャでこれを打ち消す。
    """
    monkeypatch.setenv("EXAPP_USE_LLM", "0")
