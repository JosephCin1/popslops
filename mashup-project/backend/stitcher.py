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


def stitch_clips(clip_paths: list[str], output_path: str, fade_duration: float = 0.5) -> None:
    """
    clip_paths: ordered list of file paths (mp3/m4a, mixed formats are fine)
    output_path: where the final stitched mp3 gets written
    fade_duration: crossfade length in seconds applied at every clip boundary
    """
    check_ffmpeg_installed()

    if len(clip_paths) == 0:
        raise StitchError("No clips to stitch - every track lookup must have failed.")

    if len(clip_paths) == 1:
        # Nothing to crossfade - just transcode/copy the single clip to output.
        cmd = ["ffmpeg", "-y", "-i", clip_paths[0], output_path]
        _run(cmd)
        return

    inputs = []
    for path in clip_paths:
        inputs += ["-i", path]

    filter_parts = []
    prev_label = "0:a"
    for i in range(1, len(clip_paths)):
        out_label = f"a{i}" if i < len(clip_paths) - 1 else "mixout"
        filter_parts.append(
            f"[{prev_label}][{i}:a]acrossfade=d={fade_duration}:c1=tri:c2=tri[{out_label}]"
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


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise StitchError(f"ffmpeg failed:\n{result.stderr[-2000:]}")
