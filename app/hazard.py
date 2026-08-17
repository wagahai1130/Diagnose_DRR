"""ハザードタイルのピクセル色を判定して、地点のリスクを求める。

ハザードマップポータルのラスタタイルは、色が凡例（浸水深や区域種別）を表す。
実際にタイルを取得して出現色を数え、以下の対応を確認した。

  浸水深（洪水・高潮）      津波（新凡例）              土砂災害
  F7F5A9  0.5m未満          FFFFB3  0.3m未満           E6C832  警戒区域(イエロー)
  FFD8C0  0.5〜3m           F8E1A6  0.3〜1m            A50021  特別警戒区域(レッド)
  FFB7B7  3〜5m             FFD8C0  1〜3m
  FF9191  5〜10m            以降は洪水と同じ
  F285C9  10〜20m
  DC7ADC  20m以上
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from . import gsi

# 地点そのものが区域外でも、この距離までは周辺を調べる
NEARBY_RADIUS_M = 50.0
ZOOM = 16
ALPHA_THRESHOLD = 128
COLOR_TOLERANCE = 24  # アンチエイリアスされた縁を拾うための許容差


@dataclass(frozen=True)
class Grade:
    """凡例の 1 段階。"""

    label: str
    rank: int  # 大きいほど危険
    depth_min: float | None = None
    depth_max: float | None = None


FLOOD_PALETTE: dict[tuple[int, int, int], Grade] = {
    (247, 245, 169): Grade("0.5m未満", 1, 0.0, 0.5),
    (255, 216, 192): Grade("0.5〜3m", 2, 0.5, 3.0),
    (255, 183, 183): Grade("3〜5m", 3, 3.0, 5.0),
    (255, 145, 145): Grade("5〜10m", 4, 5.0, 10.0),
    (242, 133, 201): Grade("10〜20m", 5, 10.0, 20.0),
    (220, 122, 220): Grade("20m以上", 6, 20.0, None),
}

TSUNAMI_PALETTE: dict[tuple[int, int, int], Grade] = {
    (255, 255, 179): Grade("0.3m未満", 1, 0.0, 0.3),
    (248, 225, 166): Grade("0.3〜1m", 2, 0.3, 1.0),
    (247, 245, 169): Grade("1m未満", 1, 0.0, 1.0),
    (255, 216, 192): Grade("1〜3m", 3, 1.0, 3.0),
    (255, 183, 183): Grade("3〜5m", 4, 3.0, 5.0),
    (255, 145, 145): Grade("5〜10m", 5, 5.0, 10.0),
    (242, 133, 201): Grade("10〜20m", 6, 10.0, 20.0),
    (220, 122, 220): Grade("20m以上", 7, 20.0, None),
}

SEDIMENT_PALETTE: dict[tuple[int, int, int], Grade] = {
    (230, 200, 50): Grade("警戒区域（イエローゾーン）", 1),
    (165, 0, 33): Grade("特別警戒区域（レッドゾーン）", 2),
}


@dataclass(frozen=True)
class Layer:
    key: str
    name: str
    layer_id: str
    palette: dict[tuple[int, int, int], Grade]
    kind: str  # "inundation" | "sediment"


LAYERS: tuple[Layer, ...] = (
    Layer("flood", "洪水浸水想定区域（想定最大規模）", "01_flood_l2_shinsuishin_data", FLOOD_PALETTE, "inundation"),
    Layer("tsunami", "津波浸水想定", "04_tsunami_newlegend_data", TSUNAMI_PALETTE, "inundation"),
    Layer("hightide", "高潮浸水想定区域（想定最大規模）", "03_hightide_l2_shinsuishin_data", FLOOD_PALETTE, "inundation"),
    Layer("debris_flow", "土砂災害警戒区域（土石流）", "05_dosekiryukeikaikuiki", SEDIMENT_PALETTE, "sediment"),
    Layer("steep_slope", "土砂災害警戒区域（急傾斜地の崩壊）", "05_kyukeishakeikaikuiki", SEDIMENT_PALETTE, "sediment"),
    Layer("landslide", "土砂災害警戒区域（地すべり）", "05_jisuberikeikaikuiki", SEDIMENT_PALETTE, "sediment"),
)

LAYERS_BY_KEY = {layer.key: layer for layer in LAYERS}


@dataclass
class HazardResult:
    layer: Layer
    at_point: Grade | None = None  # 地点そのもの
    nearby: Grade | None = None  # 周辺で最も危険な段階
    nearby_distance_m: float | None = None
    no_data: bool = False  # タイル自体が存在しない（想定区域外）

    @property
    def affected(self) -> bool:
        return self.at_point is not None

    @property
    def rank(self) -> int:
        if self.at_point:
            return self.at_point.rank
        return 0


def _match(pixel: tuple[int, int, int, int], palette) -> Grade | None:
    r, g, b, a = pixel
    if a < ALPHA_THRESHOLD:
        return None
    exact = palette.get((r, g, b))
    if exact:
        return exact

    best, best_distance = None, COLOR_TOLERANCE
    for (pr, pg, pb), grade in palette.items():
        distance = abs(pr - r) + abs(pg - g) + abs(pb - b)
        if distance < best_distance:
            best, best_distance = grade, distance
    return best


def classify(block: gsi.TileBlock, layer: Layer) -> HazardResult:
    if block.empty:
        return HazardResult(layer=layer, no_data=True)

    image = block.image
    width, height = image.size
    pixels = image.load()

    at_point = _match(pixels[block.px, block.py], layer.palette)

    # 周辺 NEARBY_RADIUS_M 以内で最も危険な段階を探す
    radius_px = max(1, int(NEARBY_RADIUS_M / block.meters_per_pixel))
    worst: Grade | None = None
    worst_distance: float | None = None

    step = max(1, radius_px // 20)  # 全画素は見ない（1 タイルあたり最大 ~1600 点）
    for dy in range(-radius_px, radius_px + 1, step):
        for dx in range(-radius_px, radius_px + 1, step):
            x, y = block.px + dx, block.py + dy
            if not (0 <= x < width and 0 <= y < height):
                continue
            if dx * dx + dy * dy > radius_px * radius_px:
                continue
            grade = _match(pixels[x, y], layer.palette)
            if grade is None:
                continue
            if worst is None or grade.rank > worst.rank:
                worst = grade
                worst_distance = ((dx * dx + dy * dy) ** 0.5) * block.meters_per_pixel

    return HazardResult(
        layer=layer,
        at_point=at_point,
        nearby=worst,
        nearby_distance_m=round(worst_distance, 1) if worst_distance is not None else None,
    )


def assess(
    lat: float,
    lon: float,
    layers: list[Layer],
    client: httpx.Client,
    on_progress=lambda *_: None,
) -> list[HazardResult]:
    results: list[HazardResult] = []
    for index, layer in enumerate(layers, start=1):
        on_progress(f"{layer.name} を照会しています... ({index}/{len(layers)})")
        block = gsi.fetch_hazard_block(layer.layer_id, lat, lon, ZOOM, client)
        results.append(classify(block, layer))
    return results
