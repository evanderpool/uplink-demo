"""Per-visitor ask limits for the public demo.

The demo answers questions with a paid API, so each visitor gets a small
allowance: LIMIT questions, then a wait until the oldest one ages out of
the window. State is one JSON file next to the database — good enough for
a single-instance demo, and losing it on a redeploy only resets counters
in the visitor's favor. The monthly spend cap on the API account is the
real backstop; this keeps casual use fair.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LIMIT = 5
WINDOW_SECONDS = 3 * 24 * 3600  # questions come back 3 days later


class RateLimiter:
    def __init__(self, path: Path, limit: int = LIMIT, window: int = WINDOW_SECONDS):
        self.path = Path(path)
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()

    def _load(self) -> dict[str, list[float]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): [float(t) for t in v] for k, v in raw.items()}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, list[float]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def check(self, ip: str, now: float | None = None) -> tuple[bool, str]:
        """Record one ask for `ip` if allowed.

        Returns (allowed, message). The message is user-facing and names the
        day the visitor can come back, not a technical error.
        """
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            cutoff = now - self.window
            stamps = [t for t in data.get(ip, []) if t > cutoff]
            # Prune other stale entries so the file never grows unbounded.
            data = {k: kept for k, v in data.items()
                    if (kept := [t for t in v if t > cutoff])}
            if len(stamps) >= self.limit:
                retry = min(stamps) + self.window
                when = datetime.fromtimestamp(retry, tz=timezone.utc).strftime(
                    "%B %d")
                return False, (
                    f"You've used your {self.limit} demo questions — "
                    f"they come back on {when}. Keyword search stays unlimited."
                )
            stamps.append(now)
            data[ip] = stamps
            self._save(data)
            return True, ""
