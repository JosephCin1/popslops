"""
Stitches a list of audio clips into one continuous MP3 using ffmpeg's
acrossfade filter, chained across all inputs in a single ffmpeg process.
Decoding happens once per source file and encoding happens once at the very
end, so there's no repeated lossy re-encoding of intermediate files.
"""
import subprocess
import shutil


class StitchError(Exception):
    pass


def check_ffmpeg_installed():
    if shutil.which("ffmpeg") is None:
        raise StitchError(
            "ffmpeg was not found on this system. Install it (e.g. `apt install "
            "ffmpeg` / `brew install ffmpeg`) and make sure it's on your PATH."
        )


def stitch_clips(
    clip_paths: list[str],
    output_path: str,
    fade_duration: float = 0.5,
    clip_duration: float | None = None,
) -> None:
    """
    clip_paths: ordered list of file paths (mp3/m4a, mixed formats are fine)
    output_path: where the final stitched mp3 gets written
    fade_duration: crossfade length in seconds applied at every clip boundary
    clip_duration: if set, trims every clip down to this many seconds
                   (from its start) before crossfading. Leave as None to use
                   each clip's full length (typically ~30s for a preview).
    """
    check_ffmpeg_installed()

    if len(clip_paths) == 0:
        raise StitchError("No clips to stitch - every track lookup must have failed.")

    if len(clip_paths) == 1:
        cmd = ["ffmpeg", "-y", "-i", clip_paths[0]]
        if clip_duration:
            cmd += ["-t", str(clip_duration)]
        cmd += [output_path]
        _run(cmd)
        return

    inputs = []
    for path in clip_paths:
        inputs += ["-i", path]

    filter_parts = []

    # Optionally trim each input to clip_duration before crossfading, so a
    # shorter "clip_duration" gives a shorter, punchier per-song snippet
    # instead of always using the full ~30s preview.
    if clip_duration:
        for i in range(len(clip_paths)):
            filter_parts.append(
                f"[{i}:a]atrim=0:{clip_duration},asetpts=PTS-STARTPTS[t{i}]"
            )
        source_label = lambda i: f"t{i}"
    else:
        source_label = lambda i: f"{i}:a"

    prev_label = source_label(0)
    for i in range(1, len(clip_paths)):
        out_label = f"a{i}" if i < len(clip_paths) - 1 else "mixout"
        # qsin = quarter-sine curve: keeps combined perceived loudness roughly
        # constant through the transition, so it doesn't dip toward silence in
        # the middle the way a straight triangular fade does.
        filter_parts.append(
            f"[{prev_label}][{source_label(i)}]acrossfade=d={fade_duration}:c1=qsin:c2=qsin[{out_label}]"
        )
        prev_label = out_label

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[mixout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        output_path,
    ]
    _run(cmd)


def estimate_duration_sec(track_count: int, clip_duration: float, fade_duration: float) -> float:
    """
    Rough estimate of the final mix length: each clip contributes its full
    length, but every crossfade overlaps two clips by fade_duration seconds,
    so the total is shorter than track_count * clip_duration.
    """
    if track_count <= 0:
        return 0.0
    total = track_count * clip_duration
    overlaps = max(track_count - 1, 0) * fade_duration
    return max(total - overlaps, 0.0)


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise StitchError(f"ffmpeg failed:\n{result.stderr[-2000:]}")