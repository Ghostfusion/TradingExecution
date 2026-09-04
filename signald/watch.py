"""Watch loop: poll the decisions inbox and feed artifacts to the processor.

``discover()`` handles the two watch shapes:

- **reports mode** (``watch_recursive=True``, the TradingAgents ``reports/``
  tree): finds ``research_decision.json`` files recursively (run_card.json and
  other JSON are ignored) and — when ``latest_only=True`` — keeps only the
  NEWEST decision per ticker (by file mtime), so a symbol's older runs never
  produce signals; a blocked/killed newest stays the only candidate.
- **inbox mode** (``watch_recursive=False``): flat ``*.json`` files in the
  watch dir, processed as-is.

The idempotency journal still skips repeated paths (same decision_hash).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .processor import ProcessResult, SignalProcessor

ARTIFACT_NAME = "research_decision.json"


def _ticker_of(path: Path) -> str | None:
    """Ticker from the artifact's own field, else the folder-name prefix."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        t = str(doc.get("ticker") or "").strip().upper()
        if t:
            return t
    except (json.JSONDecodeError, OSError):
        pass
    head = path.parent.name.split("_")[0].strip().upper()
    return head or None


class WatchLoop:
    def __init__(self, processor: SignalProcessor, poll_seconds: float = 10.0) -> None:
        self.processor = processor
        self.poll_seconds = max(0.5, float(poll_seconds))

    def discover(self) -> list[Path]:
        watch = Path(self.processor.cfg.watch_dir)
        if not watch.exists():
            return []
        if self.processor.cfg.watch_recursive:
            files = list(watch.rglob(ARTIFACT_NAME))
            if not self.processor.cfg.latest_only:
                return sorted(files)
            best: dict[str, tuple[float, Path]] = {}
            for f in files:
                ticker = _ticker_of(f)
                if not ticker:
                    continue
                mtime = f.stat().st_mtime
                if ticker not in best or mtime > best[ticker][0]:
                    best[ticker] = (mtime, f)
            return [v[1] for v in best.values()]
        return sorted(
            p for p in watch.iterdir()
            if p.is_file() and p.name.endswith(".json")
        )

    def run_once(self) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for artifact in self.discover():
            results.append(self.processor.process(artifact))
        return results

    def run_forever(self, stop: Callable[[], bool] | None = None, on_result=None) -> None:
        while True:
            if stop is not None and stop():
                return
            for res in self.run_once():
                if on_result is not None:
                    on_result(res)
            time.sleep(self.poll_seconds)
