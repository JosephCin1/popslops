"""
Offline smoke test: fakes Spotify + the clip sources with generated tones,
then drives the whole API (project -> editor calls -> render -> download).

Run from the backend folder:   python tests/smoke_test.py
Needs ffmpeg on PATH. Touches no real network services.
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["JOB_STORAGE_DIR"] = tempfile.mkdtemp(prefix="popslop_test_")
os.environ["MAX_MIX_HOURS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import projects as P  # noqa: E402
from sources import SOURCES, Candidate  # noqa: E402

TONES = tempfile.mkdtemp(prefix="tones_")
GENRES = ["Pop", "Rock", "Dance", None]


def make_tone(freq: int, path: str, seconds: int = 30) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-ac", "2", path], check=True)


def fake_tracks(url, token):
    return [{"title": f"Song {i}", "artist": f"Artist {i % 3}", "isrc": f"ISRC{i}",
             "duration_ms": 200000, "spotify_id": f"sp{i}"} for i in range(8)]


def fake_lookup(self, track):
    i = int(track["title"].split()[-1])
    if i == 7:
        return None  # one unmatched track
    return Candidate(source="deezer", ref=str(200 + i * 40), title=track["title"], artist=track["artist"],
                     bpm=float(90 + (i * 17) % 60), genre=GENRES[i % 4], release_date=f"{2000 + i}-01-01")


def fake_search(self, track, limit=5):
    return [Candidate(source="deezer", ref=str(900 + k), title=f"{track['title']} (alt {k})",
                      artist=track["artist"]) for k in range(2)]


def fake_fetch(self, ref, stem):
    path = f"{stem}.mp3"
    make_tone(int(ref), path)
    return path


P.get_playlist_tracks_with_user_token = fake_tracks
type(SOURCES["deezer"]).lookup = fake_lookup
type(SOURCES["deezer"]).search = fake_search
type(SOURCES["deezer"]).fetch_audio = fake_fetch
type(SOURCES["itunes"]).lookup = lambda self, t: None
type(SOURCES["itunes"]).search = lambda self, t, limit=5: []

client = TestClient(main.app)
main.SESSIONS["s1"] = {"access_token": "x"}


def wait(url, key="status", until=("ready", "done", "error"), timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        data = client.get(url).json()
        if data[key] in until:
            return data
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting on {url}")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


check(client.get("/api/health").json() == {"ok": True}, "health")
check(client.get("/docs").status_code == 404, "docs hidden by default")
check(client.post("/api/projects", json={"playlist_url": "x", "session_id": "nope"}).status_code == 401,
      "project needs a login")

pid = client.post("/api/projects", json={"playlist_url": "x", "session_id": "s1"}).json()["project_id"]
proj = wait(f"/api/projects/{pid}")
check(proj["status"] == "ready" and proj["total_tracks"] == 8 and proj["matched_tracks"] == 7,
      "project resolves 7/8 tracks")
check(not (P.project_dir(pid) / "clips").exists(), "no clips downloaded before anyone presses play")

tracks = proj["tracks"]
r = client.get(f"/api/projects/{pid}/tracks/t00001/audio")
check(r.status_code == 200 and len(r.content) > 1000, "lazy audio for one track")
check(len(list((P.project_dir(pid) / "clips").iterdir())) == 1, "exactly one clip on disk")
check(client.get(f"/api/projects/{pid}/tracks/t00007/audio").status_code == 404, "unmatched track has no audio")

alts = client.get(f"/api/projects/{pid}/tracks/t00002/alternatives").json()["options"]
check(len(alts) == 2, "alternatives listed")
r = client.put(f"/api/projects/{pid}/tracks/t00002/source", json={"key": alts[1]["key"]})
check(r.status_code == 200 and r.json()["matched_title"].endswith("(alt 1)"), "swap to alternative")
check(client.put(f"/api/projects/{pid}/tracks/t00002/source",
                 json={"key": "itunes:https://evil.example/x.m4a"}).status_code == 400,
      "can't inject an arbitrary URL")

up = os.path.join(TONES, "mine.mp3")
make_tone(1000, up, seconds=95)
with open(up, "rb") as f:
    r = client.post(f"/api/projects/{pid}/tracks/t00003/upload", files={"file": ("mine.mp3", f, "audio/mpeg")})
check(r.status_code == 200 and r.json()["source"] == "upload" and r.json()["seconds"] > 90, "upload own file")
with tempfile.NamedTemporaryFile(suffix=".mp3") as junk:
    junk.write(b"not audio at all")
    junk.flush()
    junk.seek(0)
    r = client.post(f"/api/projects/{pid}/tracks/t00004/upload", files={"file": ("junk.mp3", junk, "audio/mpeg")})
check(r.status_code == 400, "reject non-audio upload")

# Editor result: reversed order, track 5 removed, upload clipped from 1:00-1:20.
order = [t for t in reversed(tracks) if t["id"] != "t00005"]
items = [{"track_id": t["id"], "start": 60 if t["id"] == "t00003" else 2,
          "length": 20 if t["id"] == "t00003" else 8} for t in order]
job = client.post(f"/api/projects/{pid}/render", json={"items": items, "fade_duration": 1.0}).json()
done = wait(f"/api/jobs/{job['job_id']}")
check(done["status"] == "done", f"render finished ({done['message']})")
mp3 = client.get(done["download_url"])
check(mp3.status_code == 200 and mp3.headers["content-type"] == "audio/mpeg", "download works")
path = os.path.join(TONES, "out.mp3")
open(path, "wb").write(mp3.content)
dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", path]))
expected = done["estimated_duration_sec"]
check(abs(dur - expected) < 0.5, f"mix length {dur:.1f}s matches estimate {expected:.1f}s")

too_long = [{"track_id": "t00003", "start": 0, "length": 95}] * 40
r = client.post(f"/api/projects/{pid}/render", json={"items": too_long, "fade_duration": 0.5})
check(r.status_code == 400 and "limit" in r.json()["detail"], "length cap enforced")

legacy = client.post("/api/jobs", json={"playlist_url": "x", "session_id": "s1", "clip_duration": 5}).json()
ldone = wait(f"/api/jobs/{legacy['job_id']}")
check(ldone["status"] == "done" and client.get(ldone["download_url"]).status_code == 200, "legacy /api/jobs still works")

print("\nAll smoke tests passed.")
