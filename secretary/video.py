"""Безопасная проверка и нормализация видео для мультимодального анализа."""
from __future__ import annotations

import json
import math
import os
import resource
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .audio import _run
from .errors import VideoError

SUPPORTED_EXTENSIONS = {"mp4", "mov", "webm"}
_VIDEO_RLIMIT_AS = 1024 * 1024 * 1024
_VIDEO_RLIMIT_CPU = 180
MIME_BY_EXTENSION = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
}


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    format_name: str
    has_audio: bool


def sniff_container(head: bytes) -> str | None:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "iso-bmff"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    return None


def mime_for_file(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise VideoError(
            f"unsupported video extension {ext!r}; allowed: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS)))
    with open(path, "rb") as handle:
        kind = sniff_container(handle.read(64))
    if ext == "webm" and kind != "webm":
        raise VideoError("file does not look like valid .webm video")
    if ext in {"mp4", "mov"} and kind != "iso-bmff":
        raise VideoError(f"file does not look like valid .{ext} video")
    return MIME_BY_EXTENSION[ext]


def probe(path: Path, *, timeout: float = 30.0) -> VideoInfo:
    argv = ["ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path)]
    try:
        proc = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VideoError("ffprobe timed out") from exc
    except FileNotFoundError as exc:
        raise VideoError("ffprobe не найден") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise VideoError(f"ffprobe failed: {detail or 'no video stream'}")
    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VideoError("ffprobe returned invalid JSON") from exc
    streams = payload.get("streams") or []
    visual = next((s for s in streams if s.get("codec_type") == "video"), None)
    if visual is None:
        raise VideoError("no video stream found")
    try:
        duration = float((payload.get("format") or {}).get("duration"))
        width = int(visual.get("width"))
        height = int(visual.get("height"))
    except (TypeError, ValueError) as exc:
        raise VideoError("ffprobe did not report valid video metadata") from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise VideoError("invalid video duration or dimensions")
    return VideoInfo(
        duration=duration, width=width, height=height,
        format_name=str((payload.get("format") or {}).get("format_name", "")),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def segment_plan(duration: float, *, segment_seconds: float,
                 min_segment_seconds: float) -> list[tuple[float, float]]:
    if duration <= 0 or segment_seconds <= min_segment_seconds:
        raise VideoError("invalid video segmentation parameters")
    result: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(duration, start + segment_seconds)
        result.append((start, end))
        start = end
    if len(result) > 1 and result[-1][1] - result[-1][0] < min_segment_seconds:
        result[-2] = (result[-2][0], result[-1][1])
        result.pop()
    return result


def extract_segment(src: Path, start: float, end: float, dest: Path, *,
                    max_width: int = 960, timeout: float = 180.0) -> int:
    """Нормализует фрагмент в небольшой MP4 с речью и видеорядом."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale=min({max_width}\\,iw):-2,fps=10"
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-t", f"{max(0.05, end-start):.3f}",
        "-threads", "1", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
        "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "29",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-movflags", "+faststart", str(dest),
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout,
            preexec_fn=_video_child_limits if os.name == "posix" else None)
    except subprocess.TimeoutExpired as exc:
        dest.unlink(missing_ok=True)
        raise VideoError(f"ffmpeg timed out at {start:.1f}s") from exc
    except FileNotFoundError as exc:
        dest.unlink(missing_ok=True)
        raise VideoError("ffmpeg не найден") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        dest.unlink(missing_ok=True)
        raise VideoError(f"ffmpeg failed: {detail or 'unknown error'}")
    return dest.stat().st_size


def _video_child_limits():  # pragma: no cover - выполняется в дочернем процессе
    """Видео требует больше виртуальной памяти, но работает одним потоком."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_VIDEO_RLIMIT_AS,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (_VIDEO_RLIMIT_CPU,) * 2)
    except (ValueError, OSError):
        pass
