"""
Source registry. Order matters: automatic matching tries sources top to bottom
and keeps the first hit. Add or remove a source by editing SOURCES.
"""
from .base import AudioSource, Candidate
from .deezer import DeezerSource
from .itunes import ItunesSource
from .upload import UploadSource, probe_duration, MAX_UPLOAD_BYTES

SOURCES: dict[str, AudioSource] = {
    s.name: s for s in (DeezerSource(), ItunesSource(), UploadSource())
}


def find_best(track: dict) -> Candidate | None:
    for src in SOURCES.values():
        cand = src.lookup(track)
        if cand:
            return cand
    return None


def search_all(track: dict, limit_per_source: int = 5) -> list[Candidate]:
    out: list[Candidate] = []
    for src in SOURCES.values():
        if src.searchable:
            out.extend(src.search(track, limit=limit_per_source))
    return out


__all__ = [
    "AudioSource", "Candidate", "SOURCES", "find_best", "search_all",
    "probe_duration", "MAX_UPLOAD_BYTES",
]
