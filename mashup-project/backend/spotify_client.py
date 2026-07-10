"""
Handles talking to Spotify's Web API using the Client Credentials flow.
This flow only requires an app-level token (no user login), which is enough
to read tracks from a PUBLIC playlist. It will NOT work for private/collaborative
playlists - that would require the Authorization Code flow instead.
"""
import base64
import re
import time
import requests

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

_token_cache = {"access_token": None, "expires_at": 0}


class SpotifyError(Exception):
    pass


def extract_playlist_id(playlist_url: str) -> str:
    """
    Accepts a full Spotify playlist URL or URI and returns just the ID.
    Handles:
      https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...
      spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
      37i9dQZF1DXcBWIGoYBM5M (bare ID)
    """
    match = re.search(r"playlist[/:]([a-zA-Z0-9]+)", playlist_url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{22}", playlist_url.strip()):
        return playlist_url.strip()
    raise SpotifyError(f"Could not parse a playlist ID out of: {playlist_url}")


def get_access_token(client_id: str, client_secret: str) -> str:
    """Fetches (and caches) an app-level access token."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]

    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        headers={"Authorization": f"Basic {auth_header}"},
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise SpotifyError(f"Spotify auth failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def get_playlist_tracks(playlist_url: str, client_id: str, client_secret: str) -> list[dict]:
    """
    Returns a list of dicts: {title, artist, isrc, duration_ms, spotify_id}
    Skips local files / tracks with missing data (podcasts, removed tracks, etc.)
    """
    playlist_id = extract_playlist_id(playlist_url)
    token = get_access_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}

    tracks = []
    url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks"
    params = {
        "limit": 100,
        "fields": "items(track(name,artists(name),external_ids,duration_ms,id,is_local)),next",
    }

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 404:
            raise SpotifyError("Playlist not found. Check the link and make sure it's public.")
        if resp.status_code == 401:
            raise SpotifyError("Spotify auth token was rejected. Check your API credentials.")
        if resp.status_code != 200:
            raise SpotifyError(f"Spotify API error ({resp.status_code}): {resp.text}")

        data = resp.json()
        for item in data.get("items", []):
            track = item.get("track")
            if not track or track.get("is_local"):
                continue
            isrc = (track.get("external_ids") or {}).get("isrc")
            artists = ", ".join(a["name"] for a in track.get("artists", []))
            tracks.append({
                "title": track.get("name"),
                "artist": artists,
                "isrc": isrc,
                "duration_ms": track.get("duration_ms"),
                "spotify_id": track.get("id"),
            })

        url = data.get("next")
        params = None  # 'next' already contains full query string

    if not tracks:
        raise SpotifyError("Playlist is empty or contains no playable tracks.")

    return tracks
