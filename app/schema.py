"""入力項目の定義。

この JSON から画面の入力フォームが自動生成される。
項目を増減する場合はこのファイルだけを変更する。

対応コンポーネント: text / number / textarea / file / select / checkbox / radio / hidden

表記は常体（である調）で統一する。防災情報として断定的に記述するため。
"""

APP_NAME = "災害リスク診断"
APP_DESCRIPTION = "国土地理院の公開データによる地点別の災害リスク判定"

# 自由記述の上限。プロンプトへそのまま入るため、長さを制限する
MAX_NOTE_LENGTH = 200

REQUEST_SCHEMA: dict = {
    "address": {
        "type": "text",
        "title": "場所",
        "desc": "住所を入力する。「34.0704,134.5548」の形式で緯度・経度も指定可。",
        "required": True,
        "max_length": 200,
        "default_value": "徳島県徳島市南常三島町2-1",
    },
    "hazards": {
        "type": "checkbox",
        "title": "判定する災害",
        "desc": "未選択の場合は全項目を判定する。",
        "items": [
            {"title": "洪水", "value": "flood"},
            {"title": "津波", "value": "tsunami"},
            {"title": "高潮", "value": "hightide"},
            {"title": "土砂災害", "value": "sediment"},
        ],
    },
    "floor": {
        "type": "number",
        "title": "居住階",
        "desc": "入力した場合、垂直避難の可否を判定する。",
        "min": 1,
        "max": 60,
    },
    "conditions": {
        "type": "checkbox",
        "title": "世帯の状況",
        "desc": "避難所要時間の想定に反映する。",
        "items": [
            {"title": "高齢者・要介護者がいる", "value": "elderly"},
            {"title": "乳幼児がいる", "value": "infant"},
            {"title": "車椅子を使っている", "value": "wheelchair"},
            {"title": "自家用車がない", "value": "no_car"},
        ],
    },
    "conditions_note": {
        "type": "textarea",
        "title": "世帯の状況（自由記述）",
        "desc": f"上の選択肢に無い事情を記入する。{MAX_NOTE_LENGTH}字まで。判定結果の説明に反映する。",
        "max_length": MAX_NOTE_LENGTH,
    },
    "app_version": {
        "type": "hidden",
        "default_value": "1.1.0",
    },
}

# 画面上の「土砂災害」1 項目を、実際の 3 レイヤに展開する
HAZARD_GROUPS: dict[str, list[str]] = {
    "flood": ["flood"],
    "tsunami": ["tsunami"],
    "hightide": ["hightide"],
    "sediment": ["debris_flow", "steep_slope", "landslide"],
}


def expand_hazard_keys(selected: list[str]) -> list[str]:
    """フォームで選ばれた値 → ハザードレイヤのキー列。未選択なら全部。"""
    if not selected:
        selected = list(HAZARD_GROUPS)
    keys: list[str] = []
    for value in selected:
        for key in HAZARD_GROUPS.get(value, []):
            if key not in keys:
                keys.append(key)
    return keys
