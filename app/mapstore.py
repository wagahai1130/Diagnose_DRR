"""生成した地図を一時的に保持し、HTTP で配信できるようにする。

源内の Markdown 描画は data: URI を拒否する（react-markdown の URL 検査）。
そのため画像をインライン表示するには https の URL で配信する必要がある。

保持はメモリ上のみ。件数上限を超えた分は古いものから捨てる。
永続化しないので、再起動すると過去の地図は失われる（レポート本文は残る）。
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

MAX_ENTRIES = 200


class MapStore:
    def __init__(self) -> None:
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, png: bytes) -> str:
        """地図を保存して ID を返す。"""
        map_id = uuid.uuid4().hex
        with self._lock:
            self._items[map_id] = png
            while len(self._items) > MAX_ENTRIES:
                self._items.popitem(last=False)
        return map_id

    def get(self, map_id: str) -> bytes | None:
        with self._lock:
            return self._items.get(map_id)


store = MapStore()
