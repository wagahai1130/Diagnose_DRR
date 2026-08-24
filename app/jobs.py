"""非同期実行のジョブ管理（インメモリ）。

源内 Web は 202 応答を受け取ると SQS 経由で status_url をポーリングしに来る。
最初は 30 秒間隔、2 時間を超えると 1 分 → 5 分 → 15 分と間隔が伸び、
最大で約 96 時間まで待ってくれる。

本番運用ではプロセスが再起動すると状態が消えるので、
DynamoDB / Redis / RDB などの外部ストアに置き換えること。
"""

from __future__ import annotations

import base64
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import logic
from .normalize import Inputs

PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
COMPLETED = "COMPLETED"
ERROR = "ERROR"


def _now() -> str:
    # ISO8601 (末尾 Z)
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Job:
    request_id: str
    status: str = PENDING
    progress: str = "リクエストを受け付けました"
    outputs: str | None = None
    artifacts: list[dict] = field(default_factory=list)
    error: dict | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_status_response(self) -> dict:
        """進捗確認の応答。"""
        body: dict = {
            "request_id": self.request_id,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.status == COMPLETED:
            body["outputs"] = self.outputs or ""
            if self.artifacts:
                body["artifacts"] = self.artifacts
        if self.status == ERROR and self.error:
            body["error"] = self.error
        return body


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, request_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(request_id)

    def create(self) -> Job:
        job = Job(request_id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.request_id] = job
        return job

    def _update(self, job: Job, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = _now()

    def start(self, job: Job, inputs: Inputs, user_id: str) -> None:
        thread = threading.Thread(target=self._run, args=(job, inputs, user_id), daemon=True)
        thread.start()

    def _run(self, job: Job, inputs: Inputs, user_id: str) -> None:
        self._update(job, status=IN_PROGRESS, progress="処理を開始しました")
        try:
            result = logic.run(
                inputs,
                user_id,
                progress=lambda message: self._update(job, progress=message),
            )
            artifacts = [
                {
                    "display_name": a.display_name,
                    "contents": base64.b64encode(a.data).decode("ascii"),
                }
                for a in result.artifacts
            ]
            self._update(
                job,
                status=COMPLETED,
                progress="処理が完了しました",
                outputs=result.outputs,
                artifacts=artifacts,
            )
        except Exception as exc:  # noqa: BLE001 - 失敗理由を呼び出し元へ返すため握る
            traceback.print_exc()
            self._update(
                job,
                status=ERROR,
                progress="処理が失敗しました",
                error={"message": "処理中にエラーが発生しました。", "details": str(exc)},
            )


store = JobStore()
