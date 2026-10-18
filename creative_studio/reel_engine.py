"""Create a simple vertical slideshow Reel from photos using FFmpeg.

This is a real first rendering step (not a complete scene-aware editor). It
never downloads music or assumes that a trending song is licensed. If an audio
file is supplied, caller must ensure it is owned/licensed for the destination.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


def check_video_tools() -> dict[str, bool]:
    return {"ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None}


def probe_video(path: str | Path) -> dict[str, Any]:
    tool = shutil.which("ffprobe")
    if not tool:
        raise RuntimeError("ffprobe is not installed; install FFmpeg tools first")
    command = [tool, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()[:500]}")
    raw = json.loads(proc.stdout)
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("No video stream found")
    duration = float(raw.get("format", {}).get("duration", 0) or 0)
    return {"width": int(video.get("width", 0)), "height": int(video.get("height", 0)),
            "duration_seconds": round(duration, 3), "fps": video.get("avg_frame_rate"),
            "video_codec": video.get("codec_name"),
            "audio_present": any(s.get("codec_type") == "audio" for s in raw.get("streams", [])),
            "reel_aspect_ratio": round(int(video.get("width", 0)) / max(1, int(video.get("height", 1))), 4)}


def build_reel_plan(clip_metadata: list[dict[str, Any]], target_seconds: int = 20) -> dict[str, Any]:
    if not clip_metadata:
        raise ValueError("At least one clip metadata object is required")
    target_seconds = max(5, min(90, int(target_seconds)))
    return {"target_seconds": target_seconds, "target_aspect_ratio": "9:16",
            "steps": ["rank_assets", "fit_vertical_frame", "render_slideshow", "validate_output"],
            "clips_received": len(clip_metadata), "status": "renderable_slideshow",
            "note": "This first renderer makes a photo slideshow; semantic scene ranking and transitions remain future work."}


def render_photo_reel(image_paths: list[str | Path], output_path: str | Path, *,
                      seconds_per_photo: float = 2.5, audio_path: str | Path | None = None,
                      width: int = 1080, height: int = 1920, fps: int = 30) -> Path:
    """Render photos to a 9:16 H.264 MP4. Optional audio must be rights-cleared by caller."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed; install FFmpeg before rendering Reels")
    paths = [Path(p) for p in image_paths]
    if not paths:
        raise ValueError("At least one photo is required")
    if any(not p.is_file() for p in paths):
        raise FileNotFoundError("One or more input photos do not exist")
    seconds_per_photo = max(1.0, min(8.0, float(seconds_per_photo)))
    width, height, fps = max(360, int(width)), max(640, int(height)), max(15, min(60, int(fps)))
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="vibeos_reel_") as tmp:
        frames = []
        for idx, src in enumerate(paths):
            target = Path(tmp) / f"frame_{idx:04d}.jpg"
            with Image.open(src) as opened:
                frame = ImageOps.exif_transpose(opened).convert("RGB")
                # Cover crop to portrait, centered; preserve source file.
                ratio = max(width / frame.width, height / frame.height)
                resized = frame.resize((round(frame.width * ratio), round(frame.height * ratio)), Image.Resampling.LANCZOS)
                left = (resized.width - width) // 2
                top = (resized.height - height) // 2
                resized.crop((left, top, left + width, top + height)).save(target, "JPEG", quality=94)
            frames.append(target)

        concat = Path(tmp) / "frames.txt"
        with concat.open("w", encoding="utf-8") as handle:
            for frame in frames:
                handle.write(f"file '{frame.as_posix()}'\n")
                handle.write(f"duration {seconds_per_photo:.3f}\n")
            handle.write(f"file '{frames[-1].as_posix()}'\n")

        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                   "-i", str(concat)]
        if audio_path is not None:
            audio = Path(audio_path)
            if not audio.is_file():
                raise FileNotFoundError("The supplied audio file does not exist")
            command += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                        "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-an"]
        # veryfast + 1 thread keeps ffmpeg's RAM/CPU small enough for a 512 MB worker.
        command += ["-vf", f"fps={fps},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-threads", "1", "-crf", "20", "-movflags", "+faststart", str(out)]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        if proc.returncode:
            raise RuntimeError(f"FFmpeg render failed: {proc.stderr.strip()[-1200:]}")
    meta = probe_video(out)
    if meta["height"] <= meta["width"] or meta["duration_seconds"] <= 0:
        out.unlink(missing_ok=True)
        raise RuntimeError("Rendered video failed vertical/duration validation")
    return out
