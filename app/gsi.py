"""国土地理院・ハザードマップポータルの公開データへのアクセス。

すべて API キー不要の公開エンドポイント。実際に叩いて存在を確認済み。

  住所検索  https://msearch.gsi.go.jp/address-search/AddressSearch?q=<住所>
  標高      https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php
  基図タイル https://cyberjapandata.gsi.go.jp/xyz/<style>/{z}/{x}/{y}.png
  災害タイル https://disaportaldata.gsi.go.jp/raster/<layer>/{z}/{x}/{y}.png

タイルは「データがない場所は 404」で返る。404 は異常ではなく
「その地点は当該ハザードの想定区域に入っていない」を意味する。

出典表示: 国土地理院（ハザードマップポータルサイト）
"""

from __future__ import annotations

import io
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
ELEVATION_URL = "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php"
BASE_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/{style}/{z}/{x}/{y}.png"
HAZARD_TILE_URL = "https://disaportaldata.gsi.go.jp/raster/{layer}/{z}/{x}/{y}.png"

TILE_SIZE = 256
USER_AGENT = "genai-exapp/0.1 (disaster risk diagnosis)"
TIMEOUT = 15.0

# 国土地理院のサーバーは連続アクセスで接続を切ることがある（WinError 10054 等）。
# 一時的な失敗と「データが無い」を区別するため、通信失敗のみ再試行する。
RETRIES = 3
RETRY_WAIT_SEC = 0.6


def _get_with_retry(client: httpx.Client, url: str, **kwargs) -> httpx.Response | None:
    """通信失敗のみ再試行する。応答が返れば（404 でも）そのまま返す。"""
    for attempt in range(RETRIES):
        try:
            return client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            if attempt == RETRIES - 1:
                logger.warning("取得に失敗しました (%s): %s", url.split("?")[0], exc)
                return None
            time.sleep(RETRY_WAIT_SEC * (attempt + 1))
    return None


@dataclass
class GeoPoint:
    lat: float
    lon: float
    title: str = ""


@dataclass
class Elevation:
    value: float | None
    source: str = ""


def create_client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )


# --- 座標変換 -----------------------------------------------------------


def deg_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int, int, int]:
    """緯度経度 → (タイルX, タイルY, タイル内ピクセルX, タイル内ピクセルY)"""
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return int(x), int(y), int((x % 1) * TILE_SIZE), int((y % 1) * TILE_SIZE)


def meters_per_pixel(lat: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2**zoom)


# --- 住所・標高 ---------------------------------------------------------


def geocode(query: str, client: httpx.Client | None = None) -> GeoPoint | None:
    """住所文字列 → 座標。"緯度,経度" 形式もそのまま受け付ける。"""
    query = (query or "").strip()
    if not query:
        return None

    # 「33.7,134.5」のような直接入力
    parts = query.replace("、", ",").split(",")
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            pass
        else:
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return GeoPoint(lat=lat, lon=lon, title=f"{lat:.5f}, {lon:.5f}")

    owned = client is None
    client = client or create_client()
    try:
        response = _get_with_retry(client, GEOCODE_URL, params={"q": query})
        if response is None or response.status_code != 200:
            return None
        results = response.json()
    except ValueError:
        return None
    finally:
        if owned:
            client.close()

    if not results:
        return None
    feature = results[0]
    lon, lat = feature["geometry"]["coordinates"]
    return GeoPoint(lat=lat, lon=lon, title=feature.get("properties", {}).get("title", query))


def elevation(lat: float, lon: float, client: httpx.Client | None = None) -> Elevation:
    owned = client is None
    client = client or create_client()
    try:
        response = _get_with_retry(
            client, ELEVATION_URL, params={"lon": lon, "lat": lat, "outtype": "JSON"}
        )
        if response is None or response.status_code != 200:
            return Elevation(value=None)
        data = response.json()
    except ValueError:
        return Elevation(value=None)
    finally:
        if owned:
            client.close()

    raw = data.get("elevation")
    # データがない場所は "-----" が返る
    try:
        return Elevation(value=float(raw), source=str(data.get("hsrc") or ""))
    except (TypeError, ValueError):
        return Elevation(value=None, source=str(data.get("hsrc") or ""))


# --- タイル -------------------------------------------------------------


def fetch_tile(url: str, client: httpx.Client) -> Image.Image | None:
    """404（データなし）は None を返す。"""
    response = _get_with_retry(client, url)
    if response is None or response.status_code != 200:
        return None
    try:
        return Image.open(io.BytesIO(response.content)).convert("RGBA")
    except OSError:
        return None


@dataclass
class TileBlock:
    """点を中心とした (2*radius+1)^2 枚のタイルを 1 枚に合成したもの。"""

    image: Image.Image
    px: int  # 合成画像内での対象地点のピクセル座標
    py: int
    zoom: int
    lat: float
    empty: bool  # 1 枚も取得できなかった（＝データなし）

    @property
    def meters_per_pixel(self) -> float:
        return meters_per_pixel(self.lat, self.zoom)


def fetch_block(
    url_template: str,
    lat: float,
    lon: float,
    zoom: int,
    client: httpx.Client,
    radius: int = 1,
    **url_args,
) -> TileBlock:
    tx, ty, px, py = deg_to_tile(lat, lon, zoom)
    span = 2 * radius + 1
    canvas = Image.new("RGBA", (TILE_SIZE * span, TILE_SIZE * span), (0, 0, 0, 0))

    targets = [
        (dx, dy, url_template.format(z=zoom, x=tx + dx, y=ty + dy, **url_args))
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
    ]

    with ThreadPoolExecutor(max_workers=min(9, len(targets))) as pool:
        tiles = list(pool.map(lambda t: fetch_tile(t[2], client), targets))

    found = False
    for (dx, dy, _), tile in zip(targets, tiles):
        if tile is None:
            continue
        found = True
        canvas.paste(tile, ((dx + radius) * TILE_SIZE, (dy + radius) * TILE_SIZE))

    return TileBlock(
        image=canvas,
        px=radius * TILE_SIZE + px,
        py=radius * TILE_SIZE + py,
        zoom=zoom,
        lat=lat,
        empty=not found,
    )


def fetch_hazard_block(layer: str, lat: float, lon: float, zoom: int, client, radius: int = 1):
    return fetch_block(HAZARD_TILE_URL, lat, lon, zoom, client, radius, layer=layer)


def fetch_base_block(lat: float, lon: float, zoom: int, client, radius: int = 1, style: str = "pale"):
    return fetch_block(BASE_TILE_URL, lat, lon, zoom, client, radius, style=style)
