"""AI アプリの中身 — 災害リスク診断。

住所から、標高・浸水想定・土砂災害警戒区域を国土地理院の公開データで調べ、
その地点固有のリスクを日本語で説明する。地図画像とレポートを artifact で返す。

文章生成に LLM を入れる場合は narrative.summarize() を差し替える。
ここ（run）は組み立て役なので触らなくてよい。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from . import diagnose, narrative
from .normalize import Inputs
from .schema import expand_hazard_keys


@dataclass
class Artifact:
    """生成ファイル。源内 Web ではダウンロードリンクとして表示される。"""

    display_name: str
    data: bytes


@dataclass
class Result:
    outputs: str  # Markdown 可
    artifacts: list[Artifact]


ProgressFn = Callable[[str], None]


def _safe_name(name: str) -> str:
    """ハザード名をファイル名に使える形にする。"""
    for char in '\\/:*?"<>|（）()　 ':
        name = name.replace(char, "-")
    return name.strip("-") or "map"


def _result_json(result: diagnose.Diagnosis) -> bytes:
    payload = {
        "query": result.query,
        "point": {
            "title": result.point.title,
            "lat": result.point.lat,
            "lon": result.point.lon,
        }
        if result.point
        else None,
        "elevation_m": result.elevation.value,
        "elevation_source": result.elevation.source,
        "generated_at": result.generated_at,
        "hazards": [
            {
                "key": h.layer.key,
                "name": h.layer.name,
                "at_point": h.at_point.label if h.at_point else None,
                "nearby": h.nearby.label if h.nearby else None,
                "nearby_distance_m": h.nearby_distance_m,
                "no_data": h.no_data,
            }
            for h in result.hazards
        ],
        "source": "国土地理院（ハザードマップポータルサイト）",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def run(inputs: Inputs, user_id: str, progress: ProgressFn = lambda _: None) -> Result:
    address = str(inputs.get("address") or "").strip()
    if not address:
        return Result(outputs="## 入力エラー\n\n場所を入力してください。", artifacts=[])

    floor_raw = inputs.get("floor")
    try:
        floor = int(floor_raw) if floor_raw not in (None, "") else None
    except (TypeError, ValueError):
        floor = None

    result = diagnose.run(
        query=address,
        layer_keys=expand_hazard_keys(inputs.checkbox("hazards")),
        floor=floor,
        conditions=inputs.checkbox("conditions"),
        conditions_note=str(inputs.get("conditions_note") or ""),
        progress=progress,
    )

    report = narrative.render_report(result)
    if not result.ok:
        return Result(outputs=report, artifacts=[])

    progress("レポートを出力しています...")

    # 地図はハザードごとに 1 枚。ファイル名にハザード名を入れて区別できるようにする
    artifacts = [
        Artifact(display_name=f"{index:02d}-{_safe_name(name)}.png", data=png)
        for index, (name, png) in enumerate(result.maps, start=1)
    ]
    artifacts += [
        Artifact(display_name="risk-report.md", data=report.encode("utf-8")),
        Artifact(display_name="risk-result.json", data=_result_json(result)),
    ]

    return Result(outputs=report, artifacts=artifacts)
