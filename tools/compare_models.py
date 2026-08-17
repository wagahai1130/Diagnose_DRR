"""Ollama のモデルを同じ入力で比べる。

どのモデルを既定にするかを、勘ではなく実測で決めるためのスクリプト。
本番と同じシステムプロンプト・同じ検証関数を通すので、
「速いか」だけでなく「そのまま住民に出せる文章か」まで見える。

使い方:
    python tools/compare_models.py                     # 既定の候補を比較
    python tools/compare_models.py gemma3:4b qwen3:8b  # モデルを指定
    python tools/compare_models.py --runs 3            # ばらつきを見る

事前に `ollama pull <model>` で取得しておくこと。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import hazard, llm, narrative  # noqa: E402
from app.diagnose import Diagnosis  # noqa: E402
from app.gsi import Elevation, GeoPoint  # noqa: E402
from app.hazard import FLOOD_PALETTE, SEDIMENT_PALETTE, HazardResult  # noqa: E402

DEFAULT_CANDIDATES = ["gemma3:4b", "qwen3:8b"]


def _diagnosis(title, elevation, hazards, floor=None, conditions=()):
    return Diagnosis(
        query=title,
        point=GeoPoint(lat=34.0, lon=134.0, title=title),
        elevation=Elevation(value=elevation, source="1m（レーザ）"),
        hazards=hazards,
        floor=floor,
        conditions=list(conditions),
    )


def _at(key, grade):
    return HazardResult(layer=hazard.LAYERS_BY_KEY[key], at_point=grade)


def _near(key, grade, distance):
    return HazardResult(
        layer=hazard.LAYERS_BY_KEY[key], nearby=grade, nearby_distance_m=distance
    )


# 本番と同じ facts_for_llm() を通す（空の項目が落ちることまで含めて再現する）
CASES = {
    "浸水あり": narrative.facts_for_llm(
        _diagnosis(
            "徳島県徳島市南常三島町二丁目１番地",
            1.4,
            [
                _at("flood", FLOOD_PALETTE[(255, 216, 192)]),
                _at("tsunami", FLOOD_PALETTE[(255, 183, 183)]),
            ],
            floor=1,
            conditions=["elderly"],
        )
    ),
    "該当なし": narrative.facts_for_llm(
        _diagnosis("徳島県三好市東祖谷菅生", 912.4, [_at("flood", None)])
    ),
    "周辺のみ": narrative.facts_for_llm(
        _diagnosis(
            "高知県黒潮町上川口",
            26.9,
            [
                _near("tsunami", FLOOD_PALETTE[(255, 216, 192)], 46.0),
                _near("debris_flow", SEDIMENT_PALETTE[(165, 0, 33)], 48.0),
            ],
        )
    ),
}


def run_once(model: str, facts: dict) -> tuple[str | None, float, list[str]]:
    """1 回生成する。戻り値: (本文, 秒, 検証の指摘)"""
    original = llm.MODEL
    llm.MODEL = model
    started = time.monotonic()
    try:
        raw = llm._call_ollama(llm.build_prompt(facts))
    finally:
        llm.MODEL = original
    elapsed = time.monotonic() - started

    if raw is None:
        return None, elapsed, ["生成に失敗（モデル未取得 / Ollama 未起動）"]

    text = llm.clean(raw)
    problems = llm.verify(text, facts) if text else ["空の応答"]
    return text, elapsed, problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*", default=None, help="比較するモデル名")
    parser.add_argument("--runs", type=int, default=1, help="各モデル・各ケースの試行回数")
    parser.add_argument("--full", action="store_true", help="生成文を全文表示する")
    args = parser.parse_args()

    models = args.models or DEFAULT_CANDIDATES
    print(f"Ollama: {llm.OLLAMA_HOST}")
    print(f"比較対象: {', '.join(models)}（各 {args.runs} 回）\n")

    summary: list[tuple[str, str, float, int, int]] = []

    for model in models:
        for case_name, facts in CASES.items():
            times: list[float] = []
            passed = 0
            for attempt in range(args.runs):
                text, elapsed, problems = run_once(model, facts)
                times.append(elapsed)
                if text and not problems:
                    passed += 1

                header = f"── {model} / {case_name}"
                if args.runs > 1:
                    header += f" ({attempt + 1}/{args.runs})"
                print(header + " " + "─" * max(0, 60 - len(header)))
                print(f"   {elapsed:.1f} 秒 / {len(text or '')} 文字")
                if problems:
                    for problem in problems:
                        print(f"   ✗ {problem}")
                else:
                    print("   ✓ 検証を通過")
                if text:
                    shown = text if args.full else text[:220].replace("\n", " ")
                    print(f"   {shown}{'' if args.full else ' …'}")
                print()

            summary.append(
                (model, case_name, statistics.mean(times), passed, args.runs)
            )

    print("=" * 68)
    print(f"{'モデル':<16} {'ケース':<10} {'平均秒':>8} {'検証通過':>10}")
    print("-" * 68)
    for model, case_name, mean_time, passed, runs in summary:
        print(f"{model:<16} {case_name:<10} {mean_time:>8.1f} {f'{passed}/{runs}':>10}")
    print("=" * 68)
    print("\n決め方: 検証通過が安定していて、許容できる速度のうち最小のモデルを選ぶ。")
    print("採用するには EXAPP_LLM_MODEL=<model> を設定するか、app/llm.py の既定を書き換える。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
