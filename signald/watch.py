"""Watch loop: poll the decisions inbox and feed artifacts to the processor.

Poll + debounce semantics: every artifact is tried at most once in the sense
that a repeated copy of the same decision is skipped by the idempotency
journal (same decision_hash). Files that fail to parse are logged and skipped;
a fresh, corrected rewrite of the same path will be picked up again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .processor import ProcessResult, SignalProcessor

ARTIFACT_SUFFIXES = (".json",)


class WatchLoop:
    def __init__(self, processor: SignalProcessor, poll_seconds: float = 10.0) -> None:
        self.processor = processor
        self.poll_seconds = max(0.5, float(poll_seconds))

    def run_once(self) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        watch = Path(self.processor.cfg.watch_dir)
        if not watch.exists():
            return results
        for artifact in sorted(watch.iterdir()):
            if not artifact.is_file() or not artifact.name.endswith(ARTIFACT_SUFFIXES):
                continue
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
