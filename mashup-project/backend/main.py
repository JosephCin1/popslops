"""
PopSlop / Seam - backend API

Editor flow (current frontend):
  1. GET  /api/login -> Spotify -> /api/callback      -> session_id
  2. POST /api/projects                               -> match every track to a clip (no audio yet)
  3. GET  /api/projects/{pid}                         -> poll until status == "ready", get track list
  4. GET  /api/projects/{pid}/tracks/{tid}/audio      -> one clip, fetched only when the user presses play
     GET  /api/projects/{pid}/tracks/{tid}/alternatives
     PUT  /api/projects/{pid}/tracks/{tid}/source     -> swap to one of those alternatives
     POST /api/projects/{pid}/tracks/{tid}/upload     -> use the user's own audio file
  5. POST /api/projects/{pid}/render                  -> ordered + trimmed list -> job_id
  6. GET  /api/jobs/{job_id}, GET /api/download/{job_id}

One-shot flow (old frontend, still works):
  POST /api/jobs {playlist_url, session_id, clip_duration, fade_duration}

Run with:  uvicorn main:app --reload
"""

import os
import threading
import time
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

load_dotenv()

import projects as P  # noqa: E402  (reads env vars at import time)
from sources import MAX_UPLOAD_BYTES, probe_duration  # noqa: E402
from spotify_client import (  # noqa: E402
    SpotifyError,
    build_authorize_url,
    exchange_code_for_token,
)

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "")
FADE_DURATION = float(os.getenv("FADE_DURATION", "0.5"))
RETENTION_HOURS = float(os.getenv("FILE_RETENTION_HOURS", "24"))
# Set EXPOSE_DOCS=1 to get /docs back while developing.
EXPOSE_DOCS = os.getenv("EXPOSE_DOCS", "0") == "1"

SESSIONS: dict[str, dict] = {}

app = FastAPI(
    title="PopSlop API",
    docs_url="/docs" if EXPOSE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if EXPOSE_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your Netlify origin once things are stable
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ models --

class ProjectRequest(BaseModel):
    playlist_url: str
    session_id: str


class RenderItem(BaseModel):
    track_id: str
    start: float = 0.0
    length: float = 30.0


class RenderRequest(BaseModel):
    items: list[RenderItem] = Field(..., min_length=1)
    fade_duration: float = 0.5


class SourceChoice(BaseModel):
    key: str  # "source:ref" as returned by /alternatives


class JobRequest(BaseModel):  # legacy one-shot
    playlist_url: str
    session_id: str
    clip_duration: float = 30.0
    fade_duration: float | None = None


class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: int
    message: str
    total_tracks: int | None = None
    matched_tracks: int | None = None
    unmatched_tracks: list[str] | None = None
    estimated_duration_sec: float | None = None
    download_url: str | None = None


def _require_session(session_id: str) -> dict:
    session = SESSIONS.get(session_id)
    if not session or not session.get("access_token"):
        raise HTTPException(401, "Not logged in with Spotify (or session expired). Log in again.")
    return session


def _project_or_404(pid: str) -> dict:
    try:
        return P.get_project(pid)
    except P.ProjectError as e:
        raise HTTPException(404, str(e))


def _track_or_404(project: dict, tid: str) -> dict:
    try:
        return P.get_track(project, tid)
    except P.ProjectError as e:
        raise HTTPException(404, str(e))


def _clamp_fade(fade: float | None) -> float:
    fade = FADE_DURATION if fade is None else fade
    return max(0.2, min(fade, 10.0))


# ------------------------------------------------------------------- basics --

@app.get("/api/health")
def health():
    """Cheap endpoint the frontend pings on page load to wake a sleeping server."""
    return {"ok": True}


@app.get("/api/login")
def login():
    if not CLIENT_ID or not REDIRECT_URI:
        raise HTTPException(500, "Server is missing SPOTIFY_CLIENT_ID or SPOTIFY_REDIRECT_URI")
    state = uuid.uuid4().hex
    SESSIONS[state] = {"access_token": None}
    url = build_authorize_url(CLIENT_ID, REDIRECT_URI, state,
                              scope="playlist-read-private playlist-read-collaborative")
    return RedirectResponse(url)


@app.get("/api/callback")
def callback(code: str = Query(...), state: str = Query(...)):
    if state not in SESSIONS:
        raise HTTPException(400, "Unknown or expired login attempt")
    try:
        token_data = exchange_code_for_token(code, REDIRECT_URI, CLIENT_ID, CLIENT_SECRET)
    except SpotifyError as e:
        raise HTTPException(400, str(e))
    SESSIONS[state] = token_data
    if FRONTEND_URL:
        return RedirectResponse(f"{FRONTEND_URL}?session_id={state}")
    return {"session_id": state, "message": "Login successful."}


# ----------------------------------------------------------------- projects --

@app.post("/api/projects")
def create_project(req: ProjectRequest):
    session = _require_session(req.session_id)
    p = P.create_project(req.playlist_url, session["access_token"])
    return P.public_project(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return P.public_project(_project_or_404(pid))


@app.get("/api/projects/{pid}/tracks/{tid}/audio")
def track_audio(pid: str, tid: str):
    project = _project_or_404(pid)
    track = _track_or_404(project, tid)
    try:
        path = P.ensure_clip(project, track)
    except P.ProjectError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Couldn't fetch this clip: {e}")
    media = "audio/mpeg" if path.endswith(".mp3") else "audio/mp4" if path.endswith(".m4a") else None
    return FileResponse(path, media_type=media)


@app.get("/api/projects/{pid}/tracks/{tid}/alternatives")
def track_alternatives(pid: str, tid: str):
    project = _project_or_404(pid)
    track = _track_or_404(project, tid)
    return {"track_id": tid, "options": P.find_alternatives(project, track)}


@app.put("/api/projects/{pid}/tracks/{tid}/source")
def track_choose_source(pid: str, tid: str, choice: SourceChoice):
    project = _project_or_404(pid)
    track = _track_or_404(project, tid)
    try:
        return P.choose_alternative(project, track, choice.key)
    except P.ProjectError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{pid}/tracks/{tid}/upload")
async def track_upload(pid: str, tid: str, file: UploadFile = File(...)):
    project = _project_or_404(pid)
    track = _track_or_404(project, tid)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".webm"}:
        raise HTTPException(400, "Upload an audio file (mp3, m4a, wav, flac, ogg, opus).")

    dest_dir = P.project_dir(pid) / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{tid}_{uuid.uuid4().hex[:8]}{ext}"
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            f.write(chunk)

    seconds = probe_duration(str(dest))
    if not seconds:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "That file doesn't look like playable audio.")
    return P.attach_upload(project, track, str(dest), seconds, file.filename or "upload")


@app.post("/api/projects/{pid}/render", response_model=JobStatus)
def render(pid: str, req: RenderRequest):
    project = _project_or_404(pid)
    if project["status"] != "ready":
        raise HTTPException(409, "Project is still loading.")
    fade = _clamp_fade(req.fade_duration)
    try:
        plan = P.plan_segments(project, [i.model_dump() for i in req.items], fade)
    except P.ProjectError as e:
        raise HTTPException(400, str(e))
    return P.start_render(project, plan, fade)


# --------------------------------------------------------------------- jobs --

@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    job = P.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/download/{job_id}")
def download(job_id: str):
    job = P.JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "File not ready or job not found")
    final_path = P.job_dir(job_id) / "final_mix.mp3"
    if not final_path.exists():
        raise HTTPException(404, "File has expired or been cleaned up")
    return FileResponse(final_path, media_type="audio/mpeg", filename=f"popslop_{job_id}.mp3")


@app.post("/api/jobs", response_model=JobStatus)
def create_job_legacy(req: JobRequest):
    """Old one-shot endpoint: whole playlist, original order, same clip length for all."""
    session = _require_session(req.session_id)
    clip = max(3.0, min(req.clip_duration, 30.0))
    fade = max(0.2, min(_clamp_fade(req.fade_duration), clip / 2))
    project = P.create_project(req.playlist_url, session["access_token"])

    job_id = uuid.uuid4().hex[:12]
    P.JOBS[job_id] = {"job_id": job_id, "status": "fetching_playlist", "progress": 0,
                      "message": "Reading playlist...", "created_at": time.time()}

    def run():
        while project["status"] == "resolving":
            P.JOBS[job_id].update(progress=int(project["progress"] * 0.3), message=project["message"])
            time.sleep(1)
        if project["status"] == "error":
            P.JOBS[job_id].update(status="error", message=project["message"])
            return
        items = [{"track_id": t["id"], "start": 0, "length": clip} for t in project["tracks"]]
        try:
            plan = P.plan_segments(project, items, fade)
        except P.ProjectError as e:
            P.JOBS[job_id].update(status="error", message=str(e))
            return
        inner = P.start_render(project, plan, fade)
        while inner["status"] not in ("done", "error"):
            P.JOBS[job_id].update({k: v for k, v in inner.items() if k not in ("job_id", "progress")},
                                  progress=30 + int(inner["progress"] * 0.7))
            time.sleep(1)
        P.JOBS[job_id].update({k: v for k, v in inner.items() if k != "job_id"})

    threading.Thread(target=run, daemon=True).start()
    return P.JOBS[job_id]


# ------------------------------------------------------------------ cleanup --

def _cleanup_loop():
    while True:
        P.cleanup(RETENTION_HOURS)
        time.sleep(3600)


threading.Thread(target=_cleanup_loop, daemon=True).start()
