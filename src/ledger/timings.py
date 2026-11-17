"""Where a brief's time goes, when somebody asks.

A re-measure found briefs twice as slow from one release to the next, and the
only number was the total. With `LEDGER_TIMINGS` set to a file path (or `-`
for stderr), every brief appends one JSON line: repository, ref, total
seconds, and seconds per stage. Unset, nothing is measured or written.
"""

from __future__ import annotations

import json
import os
import sys
import time


class Stopwatch:
    """Laps of one brief. Each `lap(name)` closes the stage that ran since the last."""

    def __init__(self) -> None:
        self.on = bool(os.environ.get("LEDGER_TIMINGS"))
        self.start = self.last = time.perf_counter()
        self.stages: dict[str, float] = {}

    def lap(self, name: str) -> None:
        if not self.on:
            return
        now = time.perf_counter()
        self.stages[name] = round(self.stages.get(name, 0.0) + now - self.last, 3)
        self.last = now

    def write(self, repo: str, ref: str) -> None:
        if not self.on:
            return
        line = json.dumps({"repo": repo, "ref": ref,
                           "total": round(time.perf_counter() - self.start, 3),
                           "stages": self.stages})
        where = os.environ.get("LEDGER_TIMINGS", "")
        try:
            if where == "-":
                print(line, file=sys.stderr)
            else:
                with open(where, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass                # a measurement never fails a brief
