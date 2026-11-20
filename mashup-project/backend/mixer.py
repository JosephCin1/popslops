"""
Streaming mixer: turns an ordered list of clip segments into one MP3.

Why not one big ffmpeg filtergraph (the old stitcher.py)? With N inputs that
graph opens N files at once and chains N-1 acrossfade filters - fine for 30
songs, but a 12-hour mix is ~1,400 clips and it falls over (file handles,
memory, a giant command line).

Instead:
  1. Several ffmpeg processes decode clips in parallel, each trimmed to its
     own start/length, into raw float PCM (44.1 kHz stereo).
  2. Python crossfades neighbouring clips with the same quarter-sine (qsin)
     curve the old version used, holding at most two clips in memory.
  3. The result streams straight into ONE ffmpeg encoder process over stdin,
     so the MP3 is encoded exactly once and memory stays flat no matter how
     long the mix is.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import numpy as np

SAMPLE_RATE = 44100
CHANNELS = 2
DECODE_WORKERS = int(os.getenv("DECODE_WORKERS", "2"))
# Long mixes get a lower bitrate so a 12-hour file stays ~700 MB instead of ~1 GB.
LONG_MIX_SECONDS = float(os.getenv("LONG_MIX_SECONDS", str(2 * 3600)))
BITRATE_SHORT = os.getenv("MIX_BITRATE", "192k")
BITRATE_LONG = os.getenv("MIX_BITRATE_LONG", "128k")


class StitchError(Exception):
    pass


@dataclass
class Segment:
    path: str
    start: float = 0.0     # seconds into the source clip
    length: float = 30.0   # seconds to use


def check_ffmpeg_installed() -> None:
    if shutil.which("ffmpeg") is None:
        raise StitchError("ffmpeg was not found on this system. Install it and make sure it's on PATH.")


def estimate_duration_sec(lengths: list[float], fade_duration: float) -> float:
    """Total mix length: sum of clip lengths minus one overlap per crossfade."""
    if not lengths:
        return 0.0
    overlaps = 0.0
    for a, b in zip(lengths, lengths[1:]):
        overlaps += min(fade_duration, a / 2, b / 2)
    return max(sum(lengths) - overlaps, 0.0)


def _decode(seg: Segment) -> np.ndarray:
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-ss", f"{max(seg.start, 0):.3f}", "-t", f"{max(seg.length, 0.1):.3f}",
        "-i", seg.path,
        "-f", "f32le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise StitchError(f"Couldn't decode {os.path.basename(seg.path)}: {proc.stderr.decode()[-300:]}")
    audio = np.frombuffer(proc.stdout, dtype="<f4")
    usable = len(audio) - (len(audio) % CHANNELS)
    return audio[:usable].reshape(-1, CHANNELS)


def _curves(n: int) -> tuple[np.ndarray, np.ndarray]:
    # Same shape as ffmpeg's acrossfade c1=qsin:c2=qsin.
    x = (np.arange(n, dtype=np.float32) + 0.5) / n
    fade_in = np.sin(x * np.pi / 2)[:, None]
    fade_out = np.cos(x * np.pi / 2)[:, None]
    return fade_in, fade_out


def render_mix(
    segments: list[Segment],
    output_path: str,
    fade_duration: float = 0.5,
    bitrate: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    workers: int = DECODE_WORKERS,
) -> None:
    check_ffmpeg_installed()
    if not segments:
        raise StitchError("No clips to stitch.")

    if bitrate is None:
        total = estimate_duration_sec([s.length for s in segments], fade_duration)
        bitrate = BITRATE_LONG if total > LONG_MIX_SECONDS else BITRATE_SHORT

    fade_samples = max(int(fade_duration * SAMPLE_RATE), 1)
    err_log = tempfile.TemporaryFile()
    encoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
         "-c:a", "libmp3lame", "-b:a", bitrate, output_path],
        stdin=subprocess.PIPE, stderr=err_log,
    )

    def write(chunk: np.ndarray) -> None:
        if len(chunk):
            encoder.stdin.write(np.clip(chunk, -1.0, 1.0).astype("<f4").tobytes())

    try:
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
            # Decode a few clips ahead of the one being mixed, in order.
            lookahead = max(workers, 1) + 1
            queue: deque = deque()
            it = iter(segments)
            for seg in it:
                queue.append(pool.submit(_decode, seg))
                if len(queue) >= lookahead:
                    break

            pending: np.ndarray | None = None
            done = 0
            while queue:
                cur = queue.popleft().result()
                nxt = next(it, None)
                if nxt is not None:
                    queue.append(pool.submit(_decode, nxt))
                done += 1

                if len(cur) == 0:
                    if on_progress:
                        on_progress(done, len(segments))
                    continue

                if pending is None:
                    pending = cur
                else:
                    f = min(fade_samples, len(pending), len(cur) // 2)
                    if f > 0:
                        fade_in, fade_out = _curves(f)
                        write(pending[:-f])
                        write(pending[-f:] * fade_out + cur[:f] * fade_in)
                        pending = cur[f:]
                    else:
                        write(pending)
                        pending = cur

                if on_progress:
                    on_progress(done, len(segments))

            if pending is not None:
                write(pending)

        encoder.stdin.close()
        if encoder.wait() != 0:
            err_log.seek(0)
            raise StitchError(f"ffmpeg encode failed: {err_log.read().decode()[-1000:]}")
    except BrokenPipeError:
        encoder.wait()
        err_log.seek(0)
        raise StitchError(f"ffmpeg encode failed: {err_log.read().decode()[-1000:]}")
    except Exception:
        encoder.kill()
        raise
    finally:
        err_log.close()
