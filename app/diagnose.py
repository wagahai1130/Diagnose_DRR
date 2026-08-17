"""診断の本体。住所を受け取り、公開データを集めて Diagnosis にまとめる。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import gsi, hazard, mapimage
from .gsi import Elevation, GeoPoint
from .hazard import HazardResult, Layer
from .schema import MAX_NOTE_LENGTH

ProgressFn = Callable[[str], None]

FLOOR_HEIGHT_M = 3.0  # 1 階あたりの高さの目安


@dataclass
class Diagnosis:
    query: str
    point: GeoPoint | None = None
    elevation: Elevation = field(default_factory=lambda: Elevation(value=None))
    hazards: list[HazardResult] = field(default_factory=list)
    maps: list[tuple[str, bytes]] = field(default_factory=list)  # (ハザード名, PNG)
    floor: int | None = None
    conditions: list[str] = field(default_factory=list)
    conditions_note: str = ""  # 利用者の自由記述（信頼できない入力として扱う）
    error: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    )

    @property
    def ok(self) -> bool:
        return self.point is not None and not self.error

    @property
    def affected(self) -> list[HazardResult]:
        """地点そのものが区域内のもの。"""
        return [h for h in self.hazards if h.at_point is not None]

    @property
    def nearby_only(self) -> list[HazardResult]:
        """地点は区域外だが、周辺に区域があるもの。"""
        return [h for h in self.hazards if h.at_point is None and h.nearby is not None]

    @property
    def max_depth(self) -> float | None:
        """浸水系ハザードの想定浸水深。**範囲の上限（最悪値）で評価する。**

        「5〜10m」を下限の 5m で評価すると避難先の階を低く見積もり、
        実際に 10m 浸水したときに水没する階を安全だと案内してしまう。
        「20m以上」のように上限が無い段階は下限を使う。
        """
        depths: list[float] = []
        for hazard_result in self.affected:
            if hazard_result.layer.kind != "inundation" or not hazard_result.at_point:
                continue
            grade = hazard_result.at_point
            depth = grade.depth_max if grade.depth_max is not None else grade.depth_min
            if depth is not None:
                depths.append(depth)
        return max(depths) if depths else None

    @property
    def max_depth_label(self) -> str:
        best: HazardResult | None = None
        for h in self.affected:
            if h.layer.kind != "inundation":
                continue
            if best is None or h.at_point.rank > best.at_point.rank:
                best = h
        return best.at_point.label if best else ""

    @property
    def in_red_zone(self) -> bool:
        return any(
            h.layer.kind == "sediment" and h.at_point and h.at_point.rank >= 2
            for h in self.affected
        )

    @property
    def safe_floor(self) -> int | None:
        """想定浸水深を上回るのに必要な階数。浸水想定がなければ None。"""
        depth = self.max_depth
        if depth is None:
            return None
        return int(depth // FLOOR_HEIGHT_M) + 2


CONDITION_LABELS = {
    "elderly": "高齢者・要介護者がいる",
    "infant": "乳幼児がいる",
    "no_car": "自家用車がない",
    "wheelchair": "車椅子を使っている",
}


def run(
    query: str,
    layer_keys: list[str],
    floor: int | None = None,
    conditions: list[str] | None = None,
    conditions_note: str = "",
    progress: ProgressFn = lambda _: None,
) -> Diagnosis:
    diagnosis = Diagnosis(
        query=query,
        floor=floor,
        conditions=conditions or [],
        conditions_note=(conditions_note or "").strip()[:MAX_NOTE_LENGTH],
    )

    layers: list[Layer] = [
        hazard.LAYERS_BY_KEY[key] for key in layer_keys if key in hazard.LAYERS_BY_KEY
    ] or list(hazard.LAYERS)

    with gsi.create_client() as client:
        progress("住所を検索しています...")
        point = gsi.geocode(query, client)
        if point is None:
            diagnosis.error = "住所を特定できませんでした。市区町村名から入力するか、「緯度,経度」形式で指定してください。"
            return diagnosis
        diagnosis.point = point

        progress(f"標高を取得しています（{point.title}）...")
        diagnosis.elevation = gsi.elevation(point.lat, point.lon, client)

        diagnosis.hazards = hazard.assess(
            point.lat, point.lon, layers, client, on_progress=progress
        )

        progress("地図を作成しています...")
        diagnosis.maps = mapimage.render_all(
            point.lat, point.lon, diagnosis.hazards, client
        )

    return diagnosis
