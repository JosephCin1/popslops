"""
User upload: the person supplies their own audio file for a track and picks
the section to use. Not searchable (it never appears as an automatic match);
it's set explicitly through POST /api/projects/{id}/tracks/{tid}/upload.

`ref` is the absolute path of the saved upload inside the project folder.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .base import AudioSource, Candidate

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "40")) * 1024 * 1024


def probe_duration(path: str) -> float | None:
    """Seconds of audio in `path`, or None if ffprobe can't read it as audio."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
        dur = float((data.get("format") or {}).get("duration") or 0)
        return dur if dur > 0 else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


class UploadSource(AudioSource):
    name = "upload"
    label = "Your file"
    searchable = False

    def lookup(self, track: dict) -> Candidate | None:
        return None

    def fetch_audio(self, ref: str, dest_stem: str) -> str:
        if not os.path.isfile(ref):
            raise RuntimeError("Uploaded file is gone (server restarted or cleaned up)")
        ext = os.path.splitext(ref)[1] or ".audio"
        path = f"{dest_stem}{ext}"
        if os.path.abspath(path) != os.path.abspath(ref):
            shutil.copyfile(ref, path)
        return path
