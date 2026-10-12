"""Raw event logs: one line per message, `<local receive time in ms>\t<raw JSON text>`.

Why raw? It is lossless: if the detectors change later, we re-run them on the same recording.
Files rotate hourly (UTC) and are gzip-compressed: data/raw/YYYY-MM-DD/events-HH.tsv.gz
"""
from __future__ import annotations

import glob
import gzip
import os
import time


class LogWriter:
    def __init__(self, root: str, flush_every: float = 1.0, prefix: str = "events") -> None:
        self.root = root
        self.prefix = prefix
        self.flush_every = flush_every
        self._path: str | None = None
        self._fh = None
        self._last_flush = time.monotonic()
        self.bytes_written = 0
        self.lines = 0

    def write(self, recv_ms: float, raw: str) -> None:
        t = time.gmtime(recv_ms / 1000.0)
        path = os.path.join(self.root, time.strftime("%Y-%m-%d", t), f"{self.prefix}-{time.strftime('%H', t)}.tsv.gz")
        if path != self._path:
            self._rotate(path)
        line = f"{recv_ms:.3f}\t{raw.replace(chr(10), ' ')}\n"
        self._fh.write(line)
        self.bytes_written += len(line)
        self.lines += 1
        now = time.monotonic()
        if now - self._last_flush >= self.flush_every:
            self._fh.flush()
            self._last_flush = now

    def _rotate(self, path: str) -> None:
        self.close()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._fh = gzip.open(path, "at", encoding="utf-8", compresslevel=3)   # append: restarts are fine
        self._path = path

    @property
    def path(self) -> str | None:
        return self._path

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            finally:
                self._fh = None


def list_logs(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    return sorted(glob.glob(os.path.join(root, "**", "*.tsv.gz"), recursive=True))


def iter_log(paths):
    """Yield (recv_ms, raw_json_text) from log files, in time order. Tolerates a half-written tail."""
    for p in paths:
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    tab = line.find("\t")
                    if tab < 0:
                        continue
                    try:
                        yield float(line[:tab]), line[tab + 1:].rstrip("\n")
                    except ValueError:
                        continue
        except (EOFError, gzip.BadGzipFile, OSError):
            continue
