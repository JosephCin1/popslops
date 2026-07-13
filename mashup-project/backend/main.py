"""
Playlist Mashup - backend API

Flow:
  1. POST /api/jobs         -> submit a Spotify playlist URL, get a job_id back
  2. GET  /api/jobs/{id}    -> poll for status/progress
  3. GET  /api/download/{id} -> download the finished mp3 once status == "done"

Run with:  uvicorn main:app --reload
"""
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from spotify_client import (
    get_playlist_tracks_with_user_token,
    build_authorize_url,
    exchange_code_for_token,
    SpotifyError,
)
from audio_sources import find_preview_clip, download_clip
from stitcher import stitch_clips, StitchError

load_dotenv()

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
# e.g. https://popslop.onrender.com/api/callback - must exactly match what's
# registered in the Spotify dashboard.
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "")
# e.g. https://popslop.netlify.app - where we send the browser back to once login finishes.
FRONTEND_URL = os.getenv("FRONTEND_URL", "")
STORAGE_DIR = Path(os.getenv("JOB_STORAGE_DIR", "./job_storage"))
FADE_DURATION = float(os.getenv("FADE_DURATION", "0.5"))
RETENTION_HOURS = float(os.getenv("FILE_RETENTION_HOURS", "24"))

# In-memory session store: session_id -> {access_token, refresh_token, expires_at}
# Same tradeoffs as JOBS below - fine for one process, swap for Redis if you scale out.
SESSIONS: dict[str, dict] = {}

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Playlist Mashup API")

# Loosen this to your actual frontend origin in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store. Fine for a single-process MVP; swap for Redis/a DB
# if you ever run multiple workers or need jobs to survive a restart.
JOBS: dict[str, dict] = {}


class JobRequest(BaseModel):
    playlist_url: str
    session_id: str  # obtained from the /api/login -> /api/callback flow


class JobStatus(BaseModel):
    job_id: str
    status: str          # queued | fetching_playlist | fetching_clips | stitching | done | error
    progress: int         # 0-100
    message: str
    total_tracks: int | None = None
    matched_tracks: int | None = None
    unmatched_tracks: list[str] | None = None
    download_url: str | None = None


def _job_dir(job_id: str) -> Path:
    return STORAGE_DIR / job_id


def _update(job_id: str, **fields):
    JOBS[job_id].update(fields)


def _run_job(job_id: str, playlist_url: str, access_token: str):
    job_dir = _job_dir(job_id)
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    try:
        # --- Step 1: playlist -> track metadata ---
        _update(job_id, status="fetching_playlist", progress=5, message="Reading playlist from Spotify...")
        tracks = get_playlist_tracks_with_user_token(playlist_url, access_token)
        _update(job_id, total_tracks=len(tracks))

        # --- Step 2: track metadata -> preview clips (Deezer, then iTunes) ---
        _update(job_id, status="fetching_clips", progress=15, message=f"Found {len(tracks)} tracks. Fetching preview clips...")
        clip_paths = []
        unmatched = []

        for i, track in enumerate(tracks):
            label = f"{track['artist']} - {track['title']}"
            match = find_preview_clip(track)

            if match["preview_url"]:
                ext = "mp3" if match["format"] == "mp3" else "m4a"
                dest = clips_dir / f"{i:03d}.{ext}"
                try:
                    download_clip(match["preview_url"], str(dest))
                    clip_paths.append(str(dest))
                except Exception:
                    unmatched.append(label)
            else:
                unmatched.append(label)

            progress = 15 + int(50 * (i + 1) / len(tracks))
            _update(
                job_id,
                progress=progress,
                message=f"Fetched clip {i + 1}/{len(tracks)}...",
                matched_tracks=len(clip_paths),
                unmatched_tracks=unmatched,
            )

        if not clip_paths:
            raise StitchError("None of the tracks in this playlist could be matched on Deezer or iTunes.")

        # --- Step 3: stitch all clips into one continuous mp3 ---
        _update(job_id, status="stitching", progress=70, message="Stitching clips with crossfades...")
        final_path = job_dir / "final_mix.mp3"
        stitch_clips(clip_paths, str(final_path), fade_duration=FADE_DURATION)

        # Raw clips are no longer needed once the final file exists.
        shutil.rmtree(clips_dir, ignore_errors=True)

        _update(
            job_id,
            status="done",
            progress=100,
            message="Done.",
            download_url=f"/api/download/{job_id}",
        )

    except SpotifyError as e:
        _update(job_id, status="error", message=str(e))
    except StitchError as e:
        _update(job_id, status="error", message=str(e))
    except Exception as e:
        _update(job_id, status="error", message=f"Unexpected error: {e}")


@app.get("/api/login")
def login():
    if not CLIENT_ID or not REDIRECT_URI:
        raise HTTPException(500, "Server is missing SPOTIFY_CLIENT_ID or SPOTIFY_REDIRECT_URI")

    state = uuid.uuid4().hex
    # Pre-register the pending session so the callback has somewhere to write to.
    SESSIONS[state] = {"access_token": None}
    authorize_url = build_authorize_url(
        CLIENT_ID, REDIRECT_URI, state, scope="playlist-read-private playlist-read-collaborative"
    )
    return RedirectResponse(authorize_url)


@app.get("/api/callback")
def callback(code: str = Query(...), state: str = Query(...)):
    if state not in SESSIONS:
        raise HTTPException(400, "Unknown or expired login attempt")

    try:
        token_data = exchange_code_for_token(code, REDIRECT_URI, CLIENT_ID, CLIENT_SECRET)
    except SpotifyError as e:
        raise HTTPException(400, str(e))

    SESSIONS[state] = token_data  # state doubles as the session_id from here on

    if FRONTEND_URL:
        return RedirectResponse(f"{FRONTEND_URL}?session_id={state}")
    return {"session_id": state, "message": "Login successful. Pass this session_id to /api/jobs."}


@app.post("/api/jobs", response_model=JobStatus)
def create_job(req: JobRequest):
    session = SESSIONS.get(req.session_id)
    if not session or not session.get("access_token"):
        raise HTTPException(401, "Not logged in with Spotify (or session expired). Log in again.")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "message": "Queued...",
        "created_at": time.time(),
    }

    thread = threading.Thread(
        target=_run_job, args=(job_id, req.playlist_url, session["access_token"]), daemon=True
    )
    thread.start()

    return JOBS[job_id]


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "File not ready or job not found")

    final_path = _job_dir(job_id) / "final_mix.mp3"
    if not final_path.exists():
        raise HTTPException(404, "File has expired or been cleaned up")

    return FileResponse(final_path, media_type="audio/mpeg", filename=f"mashup_{job_id}.mp3")


def _cleanup_loop():
    """Background sweep: delete job folders older than FILE_RETENTION_HOURS."""
    while True:
        cutoff = time.time() - RETENTION_HOURS * 3600
        for job_id, job in list(JOBS.items()):
            if job.get("created_at", 0) < cutoff:
                shutil.rmtree(_job_dir(job_id), ignore_errors=True)
                JOBS.pop(job_id, None)
        time.sleep(3600)  # check hourly


threading.Thread(target=_cleanup_loop, daemon=True).start()