"""
Shared pieces for every audio source.

A "source" is anything that can turn a track (title / artist / ISRC) into a
playable audio clip. Each one lives in its own module in this package and
exposes the small interface described on `AudioSource` below. To add a new
source: copy deezer.py, implement the methods, and register it in
sources/__init__.py. To remove one: delete it from that registry.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict

import requests

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "popslop/2.0"})


@dataclass
class Candidate:
    """One possible clip for a track, as returned by a source."""
    source: str                    # registry key, e.g. "deezer"
    ref: str                       # opaque id the source uses to fetch audio later
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    seconds: float = 30.0          # how much audio this clip has (previews are ~30s)
    preview_url: str | None = None  # direct, short-lived URL for in-browser listening
    bpm: float | None = None
    genre: str | None = None
    release_date: str | None = None  # "YYYY-MM-DD" or "YYYY"
    ext: str = "mp3"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class AudioSource:
    """Interface every source implements."""
    name: str = "base"
    label: str = "Base"
    searchable: bool = True  # shows up in the "swap clip" panel

    def lookup(self, track: dict) -> Candidate | None:
        """Best single match for a Spotify track, or None."""
        raise NotImplementedError

    def search(self, track: dict, limit: int = 5) -> list[Candidate]:
        """Several alternative matches, for the swap panel."""
        return []

    def fetch_audio(self, ref: str, dest_stem: str) -> str:
        """Download/copy the clip for `ref` to dest_stem + extension; return the path."""
        raise NotImplementedError


class RateLimiter:
    """Thread-safe sliding-window limiter: at most `max_calls` per `period` seconds."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self.period]
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0]) + 0.01
            time.sleep(max(sleep_for, 0.01))


def download_url(url: str, dest_path: str, timeout: float = 20) -> None:
    resp = HTTP.get(url, timeout=timeout)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
