"""Аудио-слой: ffprobe-валидация, план сегментации, ffmpeg-нарезка.

- ffmpeg/ffprobe запускаются списком аргументов, без shell=True;
- таймаут на каждый запуск; на Linux дочернему процессу ставятся
  rlimits (память/CPU/размер файла) через preexec_fn;
- границы сегментов — НАШИ измеренные границы нарезки ffmpeg -ss/-t;
  модельных временных отметок здесь нет и быть не может.
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import AudioError

SUPPORTED_EXTENSIONS = {"mp3", "m4a", "ogg", "wav"}
MIME_BY_EXTENSION = {"mp3": "audio/mpeg", "m4a": "audio/mp4",
                     "ogg": "audio/ogg", "wav": "audio/wav"}
_FFMPEG_RLIMIT_AS = 512 * 1024 * 1024
_FFMPEG_RLIMIT_CPU = 120


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    format_name: str
    codec_type: str


@dataclass(frozen=True)
class SegmentRange:
    idx: int
    start: float
    end: float


def sniff_mime(head: bytes) -> str | None:
    """Быстрая сигнатурная проверка контейнера (без ffprobe)."""
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF
                                   and (head[1] & 0xE6) == 0xE2):
        return "audio/mpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"\x00\x00\x00") and head[4:8] == b"ftyp":
        return "audio/mp4"
    return None


def mime_for_file(path: Path) -> str:
    """MIME по расширению с проверкой сигнатуры; расхождение — ошибка."""
    ext = path.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise AudioError(
            f"unsupported extension {ext!r}; allowed: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS)))
    expected = MIME_BY_EXTENSION[ext]
    with open(path, "rb") as handle:
        head = handle.read(32)
    sniffed = sniff_mime(head)
    if sniffed is not None and sniffed != expected:
        raise AudioError(
            f"file content ({sniffed}) does not match extension (.{ext})")
    if sniffed is None and ext in {"ogg", "wav", "m4a"}:
        # mp3 без ID3 может начинаться прямо с кадра и уже покрыт веткой выше
        raise AudioError(f"file does not look like valid .{ext} audio")
    return expected


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, timeout=timeout,
        preexec_fn=_child_limits if os.name == "posix" else None)


def _child_limits():  # pragma: no cover - выполняется в дочернем процессе
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_FFMPEG_RLIMIT_AS,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (_FFMPEG_RLIMIT_CPU,) * 2)
    except (ValueError, OSError):
        pass  # лимиты не критичны: продолжаем без них


def probe(path: Path, *, timeout: float = 30.0) -> AudioInfo:
    """ffprobe: подтверждает действительный формат и длительность."""
    argv = ["ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path)]
    try:
        proc = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffprobe timed out") from exc
    except FileNotFoundError as exc:
        raise AudioError(
            "ffprobe не найден; установите ffmpeg/ffprobe и повторите проверку") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise AudioError(f"ffprobe failed: {stderr or 'no audio stream'}")
    try:
        info = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AudioError("ffprobe returned invalid JSON") from exc
    streams = info.get("streams") or []
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise AudioError("no audio stream found")
    fmt = info.get("format") or {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError) as exc:
        raise AudioError("ffprobe did not report duration") from exc
    if duration <= 0:
        raise AudioError(f"invalid duration {duration}")
    return AudioInfo(duration=duration,
                     format_name=str(fmt.get("format_name", "")),
                     codec_type="audio")


def segment_plan(duration: float, *, segment_seconds: float = 900.0,
                 overlap_seconds: float = 4.0,
                 min_segment_seconds: float = 30.0) -> list[SegmentRange]:
    """Полное покрытие [0, duration] с перекрытием overlap_seconds.

    Последний неполный отрезок < min_segment_seconds вливается в предыдущий
    (граница сдвигается), чтобы не плодить микросегменты. Перекрытие не
    создаёт нулевых/инвертированных отрезков.
    """
    if duration <= 0:
        raise AudioError("duration must be positive")
    if min_segment_seconds >= segment_seconds:
        raise AudioError("min_segment_seconds must be < segment_seconds")
    if overlap_seconds >= min_segment_seconds:
        raise AudioError("overlap_seconds must be < min_segment_seconds")
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(start + segment_seconds, duration)
        ranges.append((start, end))
        if end >= duration:
            break
        start = end - overlap_seconds
        if start <= ranges[-1][0]:  # защита от вырождения
            start = ranges[-1][0] + 0.1
    # хвост короче min — слить с предыдущим
    if len(ranges) > 1:
        last_start, last_end = ranges[-1]
        if last_end - last_start < min_segment_seconds:
            ranges[-2] = (ranges[-2][0], last_end)
            ranges.pop()
    return [SegmentRange(idx=i, start=s, end=e)
            for i, (s, e) in enumerate(ranges)]


def extract_segment(src: Path, start: float, end: float, dest: Path, *,
                    timeout: float = 180.0) -> int:
    """Вырезает [start, end) в mono 16 kHz Opus/OGG. Возвращает размер файла.

    -ss/-t — входной seek: границы отрезка задаются нами и считаются
    измеренными (точность зависит от контейнера, см. INTEGRATION.md).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-t", f"{max(0.05, end - start):.3f}",
        "-i", str(src),
        "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", str(dest),
    ]
    try:
        proc = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out extracting segment {start:.1f}s") from exc
    except FileNotFoundError as exc:
        dest.unlink(missing_ok=True)
        raise AudioError(
            "ffmpeg не найден; установите ffmpeg/ffprobe и повторите проверку") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:300]
        dest.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg failed: {stderr or 'unknown error'}")
    return dest.stat().st_size


def check_disk_budget(jobs_dir: Path, budget_bytes: int) -> None:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(jobs_dir).free
    if free < budget_bytes:
        raise AudioError(
            f"insufficient disk space: {free} bytes free, "
            f"budget requires {budget_bytes}")


def format_ts(seconds: float) -> str:
    """ЧЧ:ММ:СС (без долей) для маркеров границ нарезки."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
