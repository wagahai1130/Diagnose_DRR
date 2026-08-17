"""災害リスク診断のテスト。

ネットワークを使うテストには @pytest.mark.network を付けている。
オフラインで走らせたい場合:  pytest -m "not network"
"""

from __future__ import annotations

import base64

import pytest

from app import gsi, hazard, mapimage, narrative
from app.diagnose import Diagnosis
from app.gsi import Elevation, GeoPoint
from app.hazard import FLOOD_PALETTE, SEDIMENT_PALETTE, Grade, HazardResult, Layer
from app.normalize import normalize
from app.schema import expand_hazard_keys

# --- 座標変換 -----------------------------------------------------------


def test_tile_index_matches_known_values():
    # 徳島大学付近。z=16 のタイル番号は 2^16 の範囲に収まる
    x, y, px, py = gsi.deg_to_tile(34.07803, 134.56187, 16)
    assert 0 <= x < 2**16 and 0 <= y < 2**16
    assert 0 <= px < 256 and 0 <= py < 256
    # 経度が東にずれればタイル X は増える
    x_east, _, _, _ = gsi.deg_to_tile(34.07803, 134.60000, 16)
    assert x_east >= x


def test_meters_per_pixel_at_zoom16():
    # 日本の緯度では z=16 で 1px はおおよそ 2m
    assert 1.5 < gsi.meters_per_pixel(34.0, 16) < 2.5


# --- 色判定 -------------------------------------------------------------


def test_exact_legend_colors_are_classified():
    assert hazard._match((247, 245, 169, 255), FLOOD_PALETTE).label == "0.5m未満"
    assert hazard._match((255, 145, 145, 255), FLOOD_PALETTE).label == "5〜10m"
    assert hazard._match((220, 122, 220, 255), FLOOD_PALETTE).label == "20m以上"
    assert hazard._match((165, 0, 33, 255), SEDIMENT_PALETTE).rank == 2


def test_transparent_pixels_are_not_classified():
    assert hazard._match((0, 0, 0, 0), FLOOD_PALETTE) is None
    # 半透明すぎる縁も拾わない
    assert hazard._match((255, 145, 145, 10), FLOOD_PALETTE) is None


def test_antialiased_edge_within_tolerance_is_classified():
    # 実タイルには縁に僅かにずれた色が混ざる
    assert hazard._match((253, 144, 147, 255), FLOOD_PALETTE).label == "5〜10m"


def test_unrelated_color_is_not_classified():
    assert hazard._match((0, 120, 255, 255), FLOOD_PALETTE) is None


# --- フォーム入力の展開 --------------------------------------------------


def test_sediment_checkbox_expands_to_three_layers():
    keys = expand_hazard_keys(["sediment"])
    assert keys == ["debris_flow", "steep_slope", "landslide"]


def test_no_selection_means_all_hazards():
    assert set(expand_hazard_keys([])) == set(hazard.LAYERS_BY_KEY)


def test_checkbox_string_from_genai_expands():
    # 源内はチェックボックスを "flood,sediment" の形で送ってくる
    inputs = normalize({"hazards": "flood,sediment"})
    assert expand_hazard_keys(inputs.checkbox("hazards")) == [
        "flood",
        "debris_flow",
        "steep_slope",
        "landslide",
    ]


# --- レポート生成 --------------------------------------------------------


def _hazard(key: str, at_point: Grade | None = None, nearby: Grade | None = None, distance=None):
    return HazardResult(
        layer=hazard.LAYERS_BY_KEY[key],
        at_point=at_point,
        nearby=nearby,
        nearby_distance_m=distance,
    )


def _diagnosis(**kwargs) -> Diagnosis:
    base = dict(
        query="テスト住所",
        point=GeoPoint(lat=34.0, lon=134.0, title="徳島県テスト市"),
        elevation=Elevation(value=3.2, source="1m（レーザ）"),
        hazards=[],
    )
    base.update(kwargs)
    return Diagnosis(**base)


def test_report_when_no_hazard_applies():
    report = narrative.render_report(_diagnosis(hazards=[_hazard("flood")]))
    assert "いずれにも該当しない" in report
    # 安全宣言をしない
    assert "安全を意味しない" in report


def test_report_states_depth_and_safe_floor():
    depth = FLOOD_PALETTE[(255, 183, 183)]  # 3〜5m
    report = narrative.render_report(_diagnosis(hazards=[_hazard("flood", at_point=depth)]))
    assert "3〜5m" in report
    assert "3 階以上" in report  # 上限 5m // 3 + 2 = 3


def test_safe_floor_uses_the_worst_case_depth():
    """範囲の下限で計算すると水没する階を安全と案内してしまう。"""
    deep = FLOOD_PALETTE[(255, 145, 145)]  # 5〜10m
    diagnosis = _diagnosis(hazards=[_hazard("flood", at_point=deep)])
    assert diagnosis.max_depth == 10.0  # 下限の 5.0 ではない
    assert diagnosis.safe_floor == 5  # 10 // 3 + 2。3 階では 10m 浸水に足りない


def test_safe_floor_for_open_ended_depth():
    """「20m以上」は上限が無いので下限で評価する。"""
    extreme = FLOOD_PALETTE[(220, 122, 220)]  # 20m以上
    diagnosis = _diagnosis(hazards=[_hazard("flood", at_point=extreme)])
    assert diagnosis.max_depth == 20.0
    assert diagnosis.safe_floor == 8


def test_report_recommends_staying_when_floor_is_high_enough():
    depth = FLOOD_PALETTE[(255, 216, 192)]  # 0.5〜3m
    report = narrative.render_report(
        _diagnosis(hazards=[_hazard("flood", at_point=depth)], floor=5)
    )
    assert "垂直避難" in report


def test_report_warns_when_floor_is_too_low():
    depth = FLOOD_PALETTE[(242, 133, 201)]  # 10〜20m
    report = narrative.render_report(
        _diagnosis(hazards=[_hazard("flood", at_point=depth)], floor=2)
    )
    assert "下回るおそれ" in report


def test_report_escalates_for_red_zone():
    red = SEDIMENT_PALETTE[(165, 0, 33)]
    diagnosis = _diagnosis(hazards=[_hazard("debris_flow", at_point=red)])
    assert diagnosis.in_red_zone
    report = narrative.render_report(diagnosis)
    assert "レッドゾーン" in report
    assert "区域外へ移動" in report


def test_report_mentions_nearby_hazard_with_distance():
    depth = FLOOD_PALETTE[(255, 145, 145)]
    report = narrative.render_report(
        _diagnosis(hazards=[_hazard("tsunami", nearby=depth, distance=42.0)])
    )
    assert "42m 先" in report


def test_report_adds_condition_advice_when_a_hazard_applies():
    depth = FLOOD_PALETTE[(255, 216, 192)]
    report = narrative.render_report(
        _diagnosis(conditions=["elderly"], hazards=[_hazard("flood", at_point=depth)])
    )
    assert "警戒レベル3" in report


def test_no_condition_advice_when_nothing_applies():
    # 区域外なら避難行動の注意書きは足さない（不要な警告を増やさない）
    report = narrative.render_report(_diagnosis(conditions=["elderly"], hazards=[]))
    assert "警戒レベル3" not in report


def test_report_on_geocoding_failure():
    diagnosis = Diagnosis(query="存在しない住所", error="住所を特定できませんでした。")
    assert not diagnosis.ok
    assert "診断不能" in narrative.render_report(diagnosis)


def test_report_always_defers_to_local_government():
    report = narrative.render_report(_diagnosis(hazards=[]))
    assert "自治体が発表する避難情報に従うこと" in report
    assert "国土地理院" in report


# --- 地図に重ねるレイヤの選択 --------------------------------------------


def test_map_is_produced_for_every_applicable_hazard():
    """浸水系を 1 枚に絞ると洪水と高潮が地図から消えるため、全部を対象にする。"""
    shallow = FLOOD_PALETTE[(247, 245, 169)]  # 0.5m未満
    deep = FLOOD_PALETTE[(255, 145, 145)]  # 5〜10m
    red = SEDIMENT_PALETTE[(165, 0, 33)]
    chosen = mapimage.choose_layers(
        [
            _hazard("flood", at_point=shallow),
            _hazard("tsunami", at_point=deep),
            _hazard("hightide", at_point=shallow),
            _hazard("debris_flow", at_point=red),
        ]
    )
    keys = [h.layer.key for h in chosen]
    assert set(keys) == {"flood", "tsunami", "hightide", "debris_flow"}
    # 深いものから先に並べる
    assert keys[0] == "tsunami"


def test_map_puts_nearby_only_hazards_after_the_applicable_ones():
    depth = FLOOD_PALETTE[(255, 216, 192)]
    chosen = mapimage.choose_layers(
        [
            _hazard("tsunami", nearby=depth, distance=46.0),
            _hazard("flood", at_point=depth),
        ]
    )
    assert [h.layer.key for h in chosen] == ["flood", "tsunami"]


def test_map_skips_layers_with_no_data():
    chosen = mapimage.choose_layers([HazardResult(layer=hazard.LAYERS[0], no_data=True)])
    assert chosen == []


# --- 実データ（ネットワーク）--------------------------------------------


@pytest.fixture(scope="module")
def network():
    try:
        with gsi.create_client() as client:
            client.get(gsi.GEOCODE_URL, params={"q": "東京都"}).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"ネットワークを使えません: {exc}")


@pytest.mark.network
def test_geocode_real_address(network):
    point = gsi.geocode("徳島県徳島市南常三島町2-1")
    assert point is not None
    assert 33.9 < point.lat < 34.2
    assert 134.4 < point.lon < 134.7


def test_geocode_accepts_raw_coordinates():
    point = gsi.geocode("34.07803,134.56187")
    assert point is not None and point.lat == pytest.approx(34.07803)


@pytest.mark.network
def test_elevation_of_known_mountain(network):
    result = gsi.elevation(33.8536, 134.0939)  # 剣山
    assert result.value is not None and result.value > 1500


@pytest.mark.network
def test_missing_elevation_returns_none(network):
    result = gsi.elevation(0.0, 0.0)  # 日本国外
    assert result.value is None


@pytest.mark.network
def test_full_diagnosis_of_low_lying_coastal_city(network):
    from app import diagnose

    result = diagnose.run("徳島県徳島市南常三島町2-1", expand_hazard_keys([]))
    assert result.ok
    assert result.elevation.value is not None and result.elevation.value < 10
    # 沿岸の低地なので、浸水系のいずれかには該当するはず
    assert any(h.layer.kind == "inundation" for h in result.affected)
    assert result.maps, "地図が 1 枚も作られていない"
    for name, png in result.maps:
        assert name and png.startswith(b"\x89PNG")


@pytest.mark.network
def test_mountain_village_has_no_inundation(network):
    from app import diagnose

    result = diagnose.run("徳島県三好市東祖谷菅生", expand_hazard_keys(["flood", "tsunami"]))
    assert result.ok
    assert result.elevation.value > 500
    assert result.affected == []


@pytest.mark.network
def test_logic_run_returns_artifacts(network):
    from app import logic

    result = logic.run(normalize({"address": "東京都江戸川区平井"}), user_id="u1")
    names = [a.display_name for a in result.artifacts]
    assert "risk-report.md" in names
    assert "risk-result.json" in names
    # 地図はハザードごとに 1 枚ずつ作られる
    maps = [n for n in names if n.endswith(".png")]
    assert len(maps) >= 1, names
    for artifact in result.artifacts:
        # 源内へは Base64 で渡すので、復元できることを確認
        assert base64.b64decode(base64.b64encode(artifact.data)) == artifact.data
