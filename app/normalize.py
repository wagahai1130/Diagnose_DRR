"""受け取った inputs を扱いやすい形に正規化する。

呼び出し元によって形が揺れる箇所があるので、ここで吸収する。

1) ファイル
   形1: inputs.files = [{"key": k, "files": [{"filename": f, "content": b64}]}]
   形2: inputs.files = [{"key": k, "contents": b64, "filename": f}]
   → 両方を受け付ける。

2) 会話履歴
   単数形 conversation_history / 複数形 conversation_histories の両方が来る
   → 両方を受け付ける。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field


@dataclass
class UploadedFile:
    filename: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    def text(self, encoding: str = "utf-8") -> str:
        return self.data.decode(encoding, errors="replace")


@dataclass
class Turn:
    """過去の1往復。"""

    input: str
    output: str
    created_date: str = ""

    @property
    def input_obj(self) -> dict:
        """input は JSON 文字列で来るので、辞書に戻せるなら戻す。"""
        try:
            parsed = json.loads(self.input)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass
class Inputs:
    """正規化済みの入力。"""

    values: dict = field(default_factory=dict)
    files: dict[str, list[UploadedFile]] = field(default_factory=dict)
    history: list[Turn] = field(default_factory=list)

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def checkbox(self, key: str) -> list[str]:
        """チェックボックスは "a,b,c" のカンマ区切り文字列で来る。"""
        raw = self.values.get(key)
        if raw is None or raw == "":
            return []
        return [v for v in str(raw).split(",") if v]

    def all_files(self) -> list[UploadedFile]:
        return [f for group in self.files.values() for f in group]


def _decode(content) -> bytes:
    if not isinstance(content, str):
        return b""
    # data URI 形式で来た場合に備えてヘッダを落とす
    if content.startswith("data:") and "," in content:
        content = content.split(",", 1)[1]
    try:
        return base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError):
        return b""


def normalize_files(raw_files) -> dict[str, list[UploadedFile]]:
    result: dict[str, list[UploadedFile]] = {}
    if not isinstance(raw_files, list):
        return result

    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "files")
        bucket = result.setdefault(key, [])

        nested = entry.get("files")
        if isinstance(nested, list):
            # 形1: {"key": ..., "files": [{"filename", "content"}]}
            for item in nested:
                if not isinstance(item, dict):
                    continue
                bucket.append(
                    UploadedFile(
                        filename=str(item.get("filename") or "unnamed"),
                        data=_decode(item.get("content") or item.get("contents")),
                    )
                )
        elif "contents" in entry or "content" in entry:
            # 形2: {"key": ..., "contents": ..., "filename": ...}
            bucket.append(
                UploadedFile(
                    filename=str(entry.get("filename") or "unnamed"),
                    data=_decode(entry.get("contents") or entry.get("content")),
                )
            )

    return {k: v for k, v in result.items() if v}


def normalize_conversation(inputs: dict) -> list[Turn]:
    raw = inputs.get("conversation_histories")
    if raw is None:
        raw = inputs.get("conversation_history")

    # textarea としてユーザーが直接書いた場合は文字列で来る
    if isinstance(raw, str):
        return [Turn(input="", output=raw)] if raw.strip() else []
    if not isinstance(raw, list):
        return []

    turns: list[Turn] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        turns.append(
            Turn(
                input=str(item.get("input") or ""),
                output=str(item.get("output") or ""),
                created_date=str(item.get("createdDate") or ""),
            )
        )
    return turns


RESERVED_KEYS = {"files", "conversation_histories", "conversation_history"}


def normalize(inputs: dict) -> Inputs:
    if not isinstance(inputs, dict):
        inputs = {}
    return Inputs(
        values={k: v for k, v in inputs.items() if k not in RESERVED_KEYS},
        files=normalize_files(inputs.get("files")),
        history=normalize_conversation(inputs),
    )
