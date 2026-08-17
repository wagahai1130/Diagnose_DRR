"""診断地点の地図画像を組み立てる（PNG）。

地理院タイル（淡色地図）の上に、該当したハザードのタイルを重ね、
対象地点にマーカー・スケールバー・凡例を描く。

凡例の日本語は OS のフォントを使う。見つからない場合は凡例を省略する
（英字で代用すると意味が伝わらないため）。
"""

from __future__ import annotations

import io
from collections import Counter

import httpx
from PIL import Image, ImageDraw, ImageFont

from . import gsi
from .hazard import ALPHA_THRESHOLD, ZOOM, Grade, HazardResult, _match

OUTPUT_SIZE = 640
OVERLAY_ALPHA = 0.65

# 凡例に日本語を描くためのフォント候補（Windows / macOS / Linux）
FONT_CANDIDATES = (
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def _fade(image: Image.Image, alpha: float) -> Image.Image:
    faded = image.copy()
    band = faded.getchannel("A").point(lambda v: int(v * alpha))
    faded.putalpha(band)
    return faded


def _draw_marker(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    for radius, width in ((14, 3), (15, 1)):
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            outline=(200, 20, 20, 255),
            width=width,
        )
    draw.line([x - 22, y, x - 6, y], fill=(200, 20, 20, 255), width=3)
    draw.line([x + 6, y, x + 22, y], fill=(200, 20, 20, 255), width=3)
    draw.line([x, y - 22, x, y - 6], fill=(200, 20, 20, 255), width=3)
    draw.line([x, y + 6, x, y + 22], fill=(200, 20, 20, 255), width=3)


def _draw_scalebar(draw: ImageDraw.ImageDraw, size: int, meters_per_pixel: float) -> None:
    target_m = 100
    length = int(target_m / meters_per_pixel)
    x0, y0 = 16, size - 24
    draw.rectangle([x0 - 6, y0 - 14, x0 + length + 6, y0 + 8], fill=(255, 255, 255, 200))
    draw.line([x0, y0, x0 + length, y0], fill=(20, 20, 20, 255), width=3)
    for x in (x0, x0 + length):
        draw.line([x, y0 - 6, x, y0 + 4], fill=(20, 20, 20, 255), width=3)
    draw.text((x0, y0 - 14), f"{target_m} m", fill=(20, 20, 20, 255))


def _draw_attribution(draw: ImageDraw.ImageDraw, size: int) -> None:
    text = "(C) GSI Japan / Hazard Map Portal"
    draw.rectangle([size - 210, 6, size - 6, 22], fill=(255, 255, 255, 200))
    draw.text((size - 204, 10), text, fill=(40, 40, 40, 255))


def collect_legend(block: gsi.TileBlock, layer) -> list[tuple[tuple[int, int, int], Grade]]:
    """タイルに実際に現れている色だけを凡例として拾う。

    使われていない段階まで並べると、その深さが存在するかのように読めるため、
    描画された色のみを対象とする。
    """
    counts: Counter = Counter()
    image = block.image
    pixels = image.load()
    width, height = image.size
    step = 4  # 全画素は見ない
    for y in range(0, height, step):
        for x in range(0, width, step):
            pixel = pixels[x, y]
            if pixel[3] < ALPHA_THRESHOLD:
                continue
            grade = _match(pixel, layer.palette)
            if grade is not None:
                counts[(pixel[0], pixel[1], pixel[2], grade)] += 1

    seen: dict[str, tuple[tuple[int, int, int], Grade]] = {}
    for (r, g, b, grade), _count in counts.most_common():
        if grade.label not in seen:
            seen[grade.label] = ((r, g, b), grade)
    return sorted(seen.values(), key=lambda item: item[1].rank)


def _draw_legend(
    image: Image.Image, entries: list[tuple[str, list[tuple[tuple[int, int, int], Grade]]]]
) -> None:
    """左下に凡例を描く。フォントが無ければ何も描かない。"""
    font = _load_font(13)
    title_font = _load_font(13)
    if font is None or not entries:
        return

    draw = ImageDraw.Draw(image)
    line_height = 19
    swatch = 13
    padding = 8

    rows: list[tuple[str, tuple[int, int, int] | None]] = []
    for layer_name, items in entries:
        rows.append((layer_name, None))
        for color, grade in items:
            rows.append((grade.label, color))

    text_width = max(
        draw.textlength(label, font=font) + (swatch + 6 if color else 0)
        for label, color in rows
    )
    box_width = int(text_width) + padding * 2
    box_height = len(rows) * line_height + padding * 2

    left = 12
    top = image.height - box_height - 40  # スケールバーの上
    draw.rectangle(
        [left, top, left + box_width, top + box_height],
        fill=(255, 255, 255, 225),
        outline=(120, 120, 120, 255),
    )

    y = top + padding
    for label, color in rows:
        if color is None:
            draw.text((left + padding, y), label, fill=(20, 20, 20, 255), font=title_font)
        else:
            draw.rectangle(
                [left + padding, y + 2, left + padding + swatch, y + 2 + swatch],
                fill=(*color, 255),
                outline=(90, 90, 90, 255),
            )
            draw.text(
                (left + padding + swatch + 6, y), label, fill=(40, 40, 40, 255), font=font
            )
        y += line_height


def choose_layers(hazards: list[HazardResult]) -> list[HazardResult]:
    """地図を作る対象のレイヤ。該当または周辺該当があるものすべて。

    以前は浸水系を最も深い 1 枚に絞っていたが、洪水と高潮が地図から消えてしまうため、
    **ハザードごとに 1 枚ずつ**作る方式に変更した（凡例の色が同じで重ねると読めないため）。
    """
    relevant = [h for h in hazards if not h.no_data and (h.at_point or h.nearby)]
    return sorted(
        relevant,
        key=lambda h: (h.at_point is None, -(h.at_point or h.nearby).rank),
    )


def _finish(
    base: gsi.TileBlock,
    canvas: Image.Image,
    legend: list[tuple[str, list[tuple[tuple[int, int, int], Grade]]]],
) -> bytes:
    """切り抜き・マーカー・スケール・凡例を描いて PNG にする。"""
    half = OUTPUT_SIZE // 2
    left = max(0, min(canvas.width - OUTPUT_SIZE, base.px - half))
    top = max(0, min(canvas.height - OUTPUT_SIZE, base.py - half))
    cropped = canvas.crop((left, top, left + OUTPUT_SIZE, top + OUTPUT_SIZE))

    draw = ImageDraw.Draw(cropped)
    _draw_marker(draw, base.px - left, base.py - top)
    _draw_scalebar(draw, OUTPUT_SIZE, base.meters_per_pixel)
    _draw_attribution(draw, OUTPUT_SIZE)
    _draw_legend(cropped, legend)

    buffer = io.BytesIO()
    cropped.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_all(
    lat: float,
    lon: float,
    hazards: list[HazardResult],
    client: httpx.Client,
    limit: int = 4,
) -> list[tuple[str, bytes]]:
    """ハザードごとに地図を 1 枚ずつ作る。戻り値は (ハザード名, PNG) の列。

    該当が何も無い場合は、地点を示すだけの地図を 1 枚返す。
    基図タイルは 1 度だけ取得して使い回す。
    """
    base = gsi.fetch_base_block(lat, lon, ZOOM, client)
    if base.empty:
        return []

    targets = choose_layers(hazards)[:limit]
    if not targets:
        return [("該当する想定区域なし", _finish(base, base.image.convert("RGBA"), []))]

    maps: list[tuple[str, bytes]] = []
    for hazard in targets:
        block = gsi.fetch_hazard_block(hazard.layer.layer_id, lat, lon, ZOOM, client)
        if block.empty:
            continue
        canvas = Image.alpha_composite(
            base.image.convert("RGBA"), _fade(block.image, OVERLAY_ALPHA)
        )
        legend = []
        items = collect_legend(block, hazard.layer)
        if items:
            legend.append((hazard.layer.name, items))
        maps.append((hazard.layer.name, _finish(base, canvas, legend)))

    return maps
