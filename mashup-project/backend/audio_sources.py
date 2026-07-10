"""
Fetches a short preview clip for a track from Deezer first, falling back to
iTunes when Deezer has no match. Both return a direct, unauthenticated URL to
a ~30-second preview clip - no scraping or stream-ripping involved.
"""
import time
import requests

DEEZER_ISRC_URL = "https://api.deezer.com/track/isrc:{isrc}"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

# iTunes caps unauthenticated search at ~20 req/min. We track calls so we can
# self-throttle instead of getting temporarily blocked mid-job.
_itunes_call_times: list[float] = []
ITUNES_MAX_CALLS_PER_MINUTE = 18  # a little under the real limit, for safety


def _throttle_itunes():
    now = time.time()
    _itunes_call_times[:] = [t for t in _itunes_call_times if now - t < 60]
    if len(_itunes_call_times) >= ITUNES_MAX_CALLS_PER_MINUTE:
        sleep_for = 60 - (now - _itunes_call_times[0]) + 0.5
        time.sleep(max(sleep_for, 0))
        _itunes_call_times[:] = []
    _itunes_call_times.append(time.time())


def _try_deezer(isrc: str) -> dict | None:
    if not isrc:
        return None
    try:
        resp = requests.get(DEEZER_ISRC_URL.format(isrc=isrc), timeout=8)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    if not data or "error" in data or not data.get("preview"):
        return None

    return {
        "preview_url": data["preview"],
        "matched_title": data.get("title"),
        "matched_artist": (data.get("artist") or {}).get("name"),
        "source": "deezer",
        "format": "mp3",
    }


def _try_itunes(title: str, artist: str) -> dict | None:
    if not title:
        return None
    _throttle_itunes()
    try:
        resp = requests.get(
            ITUNES_SEARCH_URL,
            params={"term": f"{artist} {title}", "media": "music", "limit": 1},
            timeout=8,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    results = data.get("results", [])
    if not results or not results[0].get("previewUrl"):
        return None

    top = results[0]
    return {
        "preview_url": top["previewUrl"],
        "matched_title": top.get("trackName"),
        "matched_artist": top.get("artistName"),
        "source": "itunes",
        "format": "m4a",
    }


def find_preview_clip(track: dict) -> dict:
    """
    track: {title, artist, isrc, ...}
    Returns a dict with preview_url/source/format, or {"preview_url": None} if
    no match was found on either service.
    """
    result = _try_deezer(track.get("isrc"))
    if result:
        return result

    result = _try_itunes(track.get("title"), track.get("artist"))
    if result:
        return result

    return {"preview_url": None, "source": None, "format": None}


def download_clip(preview_url: str, dest_path: str) -> None:
    resp = requests.get(preview_url, timeout=15)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
