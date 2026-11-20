"""
iTunes Search API: fallback source. Slow (Apple allows ~20 requests/minute
unauthenticated), so it only runs for tracks Deezer couldn't match, and for
the swap panel when a user asks for alternatives.
"""
from __future__ import annotations

from urllib.parse import urlparse

import requests

from .base import AudioSource, Candidate, RateLimiter, HTTP, download_url

SEARCH_URL = "https://itunes.apple.com/search"
_limiter = RateLimiter(max_calls=18, period=60.0)

# Preview URLs are stored as `ref`, so only ever download from Apple's hosts.
_ALLOWED_HOST_SUFFIXES = (".apple.com", ".mzstatic.com")


def _to_candidate(r: dict) -> Candidate | None:
    if not r.get("previewUrl"):
        return None
    return Candidate(
        source="itunes",
        ref=r["previewUrl"],
        title=r.get("trackName"),
        artist=r.get("artistName"),
        album=r.get("collectionName"),
        seconds=30.0,
        preview_url=r["previewUrl"],
        genre=r.get("primaryGenreName"),
        release_date=(r.get("releaseDate") or "")[:10] or None,
        ext="m4a",
    )


class ItunesSource(AudioSource):
    name = "itunes"
    label = "iTunes"

    def lookup(self, track: dict) -> Candidate | None:
        hits = self.search(track, limit=1)
        return hits[0] if hits else None

    def search(self, track: dict, limit: int = 5) -> list[Candidate]:
        title = track.get("title")
        if not title:
            return []
        artist = (track.get("artist") or "").split(",")[0]
        _limiter.wait()
        try:
            resp = HTTP.get(
                SEARCH_URL,
                params={"term": f"{artist} {title}", "media": "music", "entity": "song", "limit": limit},
                timeout=8,
            )
            results = resp.json().get("results", [])
        except (requests.RequestException, ValueError):
            return []
        return [c for c in (_to_candidate(r) for r in results) if c]

    def fetch_audio(self, ref: str, dest_stem: str) -> str:
        host = urlparse(ref).hostname or ""
        if urlparse(ref).scheme != "https" or not host.endswith(_ALLOWED_HOST_SUFFIXES):
            raise RuntimeError("Refusing to download a non-Apple iTunes preview URL")
        path = f"{dest_stem}.m4a"
        download_url(ref, path)
        return path
