from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any
import uuid

from .models import D365LogConfig


class StructuredLogger:
    def __init__(self, config: D365LogConfig) -> None:
        self._run_id = uuid.uuid4().hex
        self._counter = 0
        self._lock = threading.Lock()
        if config.log_path:
            self.log_path = Path(config.log_path)
        else:
            utc_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.log_path = Path(config.log_dir) / f"crm_push_{utc_day}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        event_type: str,
        outcome: str,
        table_name: str | None = None,
        record_index: int | None = None,
        **details: Any,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._counter += 1
            event: dict[str, Any] = {
                "timestamp_utc": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "sort_key": f"{int(now.timestamp() * 1000):013d}-{self._counter:06d}",
                "run_id": self._run_id,
                "event_type": event_type,
                "outcome": outcome,
            }
            if table_name is not None:
                event["table_name"] = table_name
            if record_index is not None:
                event["record_index"] = record_index
            for key, value in details.items():
                if value is not None:
                    event[key] = value
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=True) + "\n")
