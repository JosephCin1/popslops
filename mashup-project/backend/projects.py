"""
Projects = one playlist being edited.

  create_project()  reads the Spotify playlist and matches every track to a
                    clip source in parallel, WITHOUT downloading any audio.
  ensure_clip()     downloads a single track's clip on demand (when the user
                    presses play in the editor, or at render time).
  run_render()      downloads whatever is still missing, in parallel, then
                    hands the ordered segments to the streaming mixer.

Everything is kept in memory (PROJECTS / JOBS), same trade-off as before:
fine for one server process; a restart forgets projects in progress.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from mixer import Segment, StitchError, estimate_duration_sec, render_mix
from sources import SOURCES, Candidate, find_best, search_all
from spotify_client import SpotifyError, get_playlist_tracks_with_user_token

STORAGE_DIR = Path(os.getenv("JOB_STORAGE_DIR", "./job_storage"))
RESOLVE_WORKERS = int(os.getenv("RESOLVE_WORKERS", "8"))
DOWNLOAD_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "8"))
MAX_MIX_SECONDS = float(os.getenv("MAX_MIX_HOURS", "12")) * 3600
MAX_TRACKS = int(os.getenv("MAX_TRACKS", "3000"))
# Only one render at a time: on a small server two big renders would starve each other.
_render_slots = threading.Semaphore(int(os.getenv("MAX_CONCURRENT_RENDERS", "1")))

PROJECTS: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
_clip_locks: dict[str, threading.Lock] = {}
_clip_locks_guard = threading.Lock()

STORAGE_DIR.mkdir(parents=True, exist_ok=True)


class ProjectError(Exception):
    pass


# ---------------------------------------------------------------- helpers --

def project_dir(project_id: str) -> Path:
    return STORAGE_DIR / f"p_{project_id}"


def job_dir(job_id: str) -> Path:
    return STORAGE_DIR / job_id


def _apply_candidate(track: dict, cand: Candidate | None) -> None:
    """Copy a source match onto a track. Metadata already on the track is kept
    if the new source doesn't have it (e.g. iTunes has no BPM)."""
    if cand is None:
        track.update(source=None, source_ref=None, seconds=0, matched=False)
        return
    track.update(
        source=cand.source,
        source_ref=cand.ref,
        matched_title=cand.title,
        matched_artist=cand.artist,
        seconds=cand.seconds,
        matched=True,
    )
    for key in ("bpm", "genre", "release_date"):
        val = getattr(cand, key)
        if val:
            track[key] = val
    track["version"] = track.get("version", 0) + 1  # busts the cached clip + browser audio


def public_track(t: dict) -> dict:
    keys = ("id", "original_index", "title", "artist", "duration_ms", "matched",
            "source", "matched_title", "matched_artist", "seconds", "bpm", "genre",
            "release_date", "version")
    return {k: t.get(k) for k in keys}


def public_project(p: dict) -> dict:
    return {
        "project_id": p["id"],
        "status": p["status"],          # resolving | ready | error
        "progress": p["progress"],
        "message": p["message"],
        "total_tracks": len(p["tracks"]),
        "matched_tracks": sum(1 for t in p["tracks"] if t.get("matched")),
        "tracks": [public_track(t) for t in p["tracks"]] if p["status"] == "ready" else [],
        "max_mix_seconds": MAX_MIX_SECONDS,
        "sources": {k: s.label for k, s in SOURCES.items()},
    }


def get_project(project_id: str) -> dict:
    p = PROJECTS.get(project_id)
    if not p:
        raise ProjectError("Project not found (it may have expired or the server restarted).")
    return p


def get_track(project: dict, track_id: str) -> dict:
    for t in project["tracks"]:
        if t["id"] == track_id:
            return t
    raise ProjectError("Track not found in this project.")


# --------------------------------------------------------------- resolving --

def create_project(playlist_url: str, access_token: str) -> dict:
    pid = uuid.uuid4().hex[:16]
    PROJECTS[pid] = {
        "id": pid, "status": "resolving", "progress": 0,
        "message": "Reading playlist from Spotify...",
        "tracks": [], "created_at": time.time(),
    }
    project_dir(pid).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_resolve, args=(pid, playlist_url, access_token), daemon=True).start()
    return PROJECTS[pid]


def _resolve(pid: str, playlist_url: str, access_token: str) -> None:
    p = PROJECTS[pid]
    try:
        raw = get_playlist_tracks_with_user_token(playlist_url, access_token)
        if len(raw) > MAX_TRACKS:
            raw = raw[:MAX_TRACKS]
        tracks = [{**t, "id": f"t{i:05d}", "original_index": i, "version": 0} for i, t in enumerate(raw)]
        p.update(tracks=tracks, progress=5, message=f"Found {len(tracks)} tracks. Matching clips...")

        done = 0
        with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
            futures = {pool.submit(find_best, t): t for t in tracks}
            for fut in as_completed(futures):
                track = futures[fut]
                try:
                    _apply_candidate(track, fut.result())
                except Exception:
                    _apply_candidate(track, None)
                done += 1
                p.update(progress=5 + int(95 * done / len(tracks)),
                         message=f"Matched {done}/{len(tracks)} tracks...")

        matched = sum(1 for t in tracks if t.get("matched"))
        if not matched:
            raise ProjectError("None of the tracks could be matched on Deezer or iTunes.")
        p.update(status="ready", progress=100, message=f"{matched} of {len(tracks)} tracks matched.")
    except (SpotifyError, ProjectError) as e:
        p.update(status="error", message=str(e))
    except Exception as e:  # pragma: no cover - last-resort message for the UI
        p.update(status="error", message=f"Unexpected error: {e}")


# ------------------------------------------------------------ clip fetching --

def _lock_for(key: str) -> threading.Lock:
    with _clip_locks_guard:
        return _clip_locks.setdefault(key, threading.Lock())


def ensure_clip(project: dict, track: dict) -> str:
    """Path to this track's audio, downloading it first if needed."""
    if not track.get("matched"):
        raise ProjectError("This track has no clip.")
    ref_hash = hashlib.sha1(f"{track['source']}|{track['source_ref']}".encode()).hexdigest()[:10]
    stem = str(project_dir(project["id"]) / "clips" / f"{track['id']}_{ref_hash}")
    with _lock_for(stem):
        cached = track.get("clip_path")
        if cached and cached.startswith(stem) and os.path.isfile(cached):
            return cached
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        path = SOURCES[track["source"]].fetch_audio(track["source_ref"], stem)
        track["clip_path"] = path
        return path


# ------------------------------------------------------- alternatives/swap --

def find_alternatives(project: dict, track: dict) -> list[dict]:
    cands = search_all(track)
    # Remember what we offered, so the client can only pick from this list
    # (it never gets to hand us an arbitrary URL to download).
    track["offered"] = {f"{c.source}:{c.ref}": c for c in cands}
    out = []
    for c in cands:
        d = c.to_dict()
        d["key"] = f"{c.source}:{c.ref}"
        d["current"] = (c.source == track.get("source") and c.ref == track.get("source_ref"))
        out.append(d)
    return out


def choose_alternative(project: dict, track: dict, key: str) -> dict:
    cand = (track.get("offered") or {}).get(key)
    if not cand:
        raise ProjectError("That option isn't available any more. Search again.")
    _apply_candidate(track, cand)
    track.pop("clip_path", None)
    return public_track(track)


def attach_upload(project: dict, track: dict, saved_path: str, seconds: float, filename: str) -> dict:
    cand = Candidate(source="upload", ref=saved_path, title=filename, artist="(your file)",
                     seconds=seconds, ext=os.path.splitext(saved_path)[1].lstrip("."))
    _apply_candidate(track, cand)
    track.pop("clip_path", None)
    return public_track(track)


# --------------------------------------------------------------- rendering --

def plan_segments(project: dict, items: list[dict], fade: float) -> list[tuple[dict, float, float]]:
    """Validate the editor's ordered list -> [(track, start, length)]."""
    by_id = {t["id"]: t for t in project["tracks"]}
    plan = []
    for it in items:
        t = by_id.get(it.get("track_id"))
        if not t or not t.get("matched"):
            continue
        avail = float(t.get("seconds") or 0)
        start = max(0.0, min(float(it.get("start", 0)), max(avail - 1, 0)))
        length = max(1.0, min(float(it.get("length", 30)), avail - start))
        plan.append((t, start, length))
    if not plan:
        raise ProjectError("Nothing to render: every track is removed or unmatched.")
    total = estimate_duration_sec([l for _, _, l in plan], fade)
    if total > MAX_MIX_SECONDS + 1:
        raise ProjectError(
            f"Mix would be {total / 3600:.1f} h; the limit is {MAX_MIX_SECONDS / 3600:.0f} h. "
            "Remove tracks or shorten clips."
        )
    return plan


def start_render(project: dict, plan: list[tuple[dict, float, float]], fade: float) -> dict:
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "job_id": job_id, "project_id": project["id"], "status": "queued", "progress": 0,
        "message": "Waiting for a free render slot..." , "created_at": time.time(),
        "total_tracks": len(plan), "matched_tracks": len(plan), "unmatched_tracks": [],
        "estimated_duration_sec": estimate_duration_sec([l for _, _, l in plan], fade),
    }
    threading.Thread(target=_run_render, args=(job_id, project, plan, fade), daemon=True).start()
    return JOBS[job_id]


def _run_render(job_id: str, project: dict, plan, fade: float) -> None:
    job = JOBS[job_id]
    try:
        with _render_slots:
            job.update(status="fetching_clips", progress=2, message="Downloading clips...")
            failed: list[str] = []
            paths: dict[str, str] = {}
            unique = {t["id"]: t for t, _, _ in plan}
            done = 0
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                futures = {pool.submit(ensure_clip, project, t): t for t in unique.values()}
                for fut in as_completed(futures):
                    t = futures[fut]
                    try:
                        paths[t["id"]] = fut.result()
                    except Exception:
                        failed.append(f"{t['artist']} - {t['title']}")
                    done += 1
                    job.update(progress=2 + int(38 * done / len(unique)),
                               message=f"Downloaded {done}/{len(unique)} clips...")

            segments = [Segment(paths[t["id"]], s, l) for t, s, l in plan if t["id"] in paths]
            if not segments:
                raise StitchError("Couldn't download any of the clips.")
            job.update(
                unmatched_tracks=failed, matched_tracks=len(segments),
                estimated_duration_sec=estimate_duration_sec([s.length for s in segments], fade),
                status="stitching", progress=40, message="Mixing...",
            )

            out = job_dir(job_id)
            out.mkdir(parents=True, exist_ok=True)

            def on_progress(i: int, n: int) -> None:
                job.update(progress=40 + int(59 * i / n), message=f"Mixed {i}/{n} clips...")

            render_mix(segments, str(out / "final_mix.mp3"), fade_duration=fade, on_progress=on_progress)
            job.update(status="done", progress=100, message="Done.", download_url=f"/api/download/{job_id}")
    except (StitchError, ProjectError) as e:
        job.update(status="error", message=str(e))
    except Exception as e:  # pragma: no cover
        job.update(status="error", message=f"Unexpected error: {e}")


# ----------------------------------------------------------------- cleanup --

def cleanup(retention_hours: float) -> None:
    cutoff = time.time() - retention_hours * 3600
    for jid, job in list(JOBS.items()):
        if job.get("created_at", 0) < cutoff:
            shutil.rmtree(job_dir(jid), ignore_errors=True)
            JOBS.pop(jid, None)
    for pid, p in list(PROJECTS.items()):
        if p.get("created_at", 0) < cutoff:
            shutil.rmtree(project_dir(pid), ignore_errors=True)
            PROJECTS.pop(pid, None)
