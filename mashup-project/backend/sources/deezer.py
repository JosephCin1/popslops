"""
Deezer: primary source. Matches by ISRC first (exact recording), then by a
text search. Also supplies the BPM, genre and release date used for sorting.

Preview URLs from Deezer are signed and expire, so we store the Deezer track
id as `ref` and ask for a fresh URL whenever we actually need the audio.
"""
from __future__ import annotations

import threading

import requests

from .base import AudioSource, Candidate, RateLimiter, HTTP, download_url

API = "https://api.deezer.com"
# Deezer's documented quota is 50 requests / 5 seconds per IP.
_limiter = RateLimiter(max_calls=45, period=5.0)

_album_genre_cache: dict[int, str | None] = {}
_album_lock = threading.Lock()


def _get(path: str, params: dict | None = None) -> dict | None:
    _limiter.wait()
    try:
        resp = HTTP.get(f"{API}{path}", params=params, timeout=8)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or "error" in data:
        return None
    return data


def _album_genre(album_id: int | None) -> str | None:
    if not album_id:
        return None
    with _album_lock:
        if album_id in _album_genre_cache:
            return _album_genre_cache[album_id]
    data = _get(f"/album/{album_id}")
    genres = ((data or {}).get("genres") or {}).get("data") or []
    genre = genres[0].get("name") if genres else None
    with _album_lock:
        _album_genre_cache[album_id] = genre
    return genre


def _to_candidate(t: dict, with_details: bool) -> Candidate | None:
    if not t or not t.get("id") or not t.get("preview"):
        return None
    album = t.get("album") or {}
    bpm = t.get("bpm") or None  # Deezer reports 0 when it doesn't know
    return Candidate(
        source="deezer",
        ref=str(t["id"]),
        title=t.get("title"),
        artist=(t.get("artist") or {}).get("name"),
        album=album.get("title"),
        seconds=30.0,
        preview_url=t.get("preview"),
        bpm=float(bpm) if bpm else None,
        genre=_album_genre(album.get("id")) if with_details else None,
        release_date=t.get("release_date") or album.get("release_date"),
        ext="mp3",
    )


class DeezerSource(AudioSource):
    name = "deezer"
    label = "Deezer"

    def lookup(self, track: dict) -> Candidate | None:
        isrc = track.get("isrc")
        if isrc:
            cand = _to_candidate(_get(f"/track/isrc:{isrc}"), with_details=True)
            if cand:
                return cand
        # No ISRC hit: fall back to the first text-search result, then pull
        # the full track object so we still get BPM / release date.
        hits = self._search_raw(track, limit=1)
        if hits:
            full = _get(f"/track/{hits[0]['id']}")
            return _to_candidate(full or hits[0], with_details=True)
        return None

    def search(self, track: dict, limit: int = 5) -> list[Candidate]:
        out = []
        for t in self._search_raw(track, limit=limit):
            c = _to_candidate(t, with_details=False)
            if c:
                out.append(c)
        return out

    def fetch_audio(self, ref: str, dest_stem: str) -> str:
        data = _get(f"/track/{int(ref)}")
        if not data or not data.get("preview"):
            raise RuntimeError("Deezer no longer has a preview for this track")
        path = f"{dest_stem}.mp3"
        download_url(data["preview"], path)
        return path

    @staticmethod
    def _search_raw(track: dict, limit: int) -> list[dict]:
        title = (track.get("title") or "").replace('"', "")
        artist = (track.get("artist") or "").split(",")[0].replace('"', "")
        if not title:
            return []
        q = f'artist:"{artist}" track:"{title}"' if artist else title
        data = _get("/search", {"q": q, "limit": limit})
        hits = (data or {}).get("data") or []
        if not hits and artist:  # strict search missed: try a loose one
            data = _get("/search", {"q": f"{artist} {title}", "limit": limit})
            hits = (data or {}).get("data") or []
        return hits[:limit]
