"""Ограниченная загрузка публичного YouTube-видео через yt-dlp.

Только youtube.com/youtu.be, без плейлистов, live и приватных материалов.
Файл сохраняется на диск потоково и затем проходит обычный VideoEngine.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import YouTubeImportError

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


@dataclass(frozen=True)
class YouTubeDownload:
    path: str
    video_id: str
    title: str
    duration: float
    source_url: str
    size: int


def normalize_youtube_url(raw: str) -> tuple[str, str]:
    try:
        parts = urlsplit(raw.strip())
    except ValueError as exc:
        raise YouTubeImportError("Некорректная YouTube-ссылка") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _HOSTS or parts.username or parts.password:
        raise YouTubeImportError("Разрешены только публичные HTTPS-ссылки YouTube")
    video_id = ""
    if host == "youtu.be":
        video_id = parts.path.strip("/").split("/")[0]
    elif parts.path == "/watch":
        video_id = (parse_qs(parts.query).get("v") or [""])[0]
    elif parts.path.startswith(("/shorts/", "/embed/")):
        video_id = parts.path.strip("/").split("/")[1]
    if not _ID.fullmatch(video_id):
        raise YouTubeImportError("Не удалось определить ID видео YouTube")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise YouTubeImportError("YouTube не ответил вовремя") from exc
    except OSError as exc:
        raise YouTubeImportError("Загрузчик YouTube недоступен") from exc


def download_youtube(raw_url: str, dest_dir: str | Path, *, executable: str,
                     max_duration: float, max_bytes: int,
                     timeout: float) -> YouTubeDownload:
    url, expected_id = normalize_youtube_url(raw_url)
    tool = shutil.which(executable) if "/" not in executable else executable
    if not tool or not Path(tool).is_file():
        raise YouTubeImportError("Загрузчик YouTube не установлен")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    meta_cmd = [tool, "--dump-single-json", "--skip-download", "--no-playlist",
                "--no-warnings", url]
    meta_run = _run(meta_cmd, min(timeout, 120.0))
    if meta_run.returncode != 0:
        raise YouTubeImportError("Видео недоступно или ограничено владельцем")
    try:
        meta = json.loads(meta_run.stdout)
        video_id = str(meta["id"])
        duration = float(meta["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise YouTubeImportError("YouTube вернул неполные сведения о видео") from exc
    if video_id != expected_id or not _ID.fullmatch(video_id):
        raise YouTubeImportError("YouTube вернул неожиданное видео")
    if meta.get("is_live") or meta.get("live_status") in {"is_live", "is_upcoming"}:
        raise YouTubeImportError("Прямые трансляции пока не поддерживаются")
    if duration <= 0 or duration > max_duration:
        raise YouTubeImportError(
            f"Видео длиннее разрешённых {int(max_duration // 60)} минут")

    template = str(dest / f"youtube-{video_id}.%(ext)s")
    command = [
        tool, "--no-playlist", "--no-warnings", "--restrict-filenames",
        "--max-filesize", str(max_bytes),
        # Сначала готовый объединённый поток. Если YouTube его не отдаёт,
        # берём две ограниченные дорожки; yt-dlp склеивает их без перекодировки.
        "--format", "b[height<=480][ext=mp4]/bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480]",
        "--merge-output-format", "mp4", "--output", template, url,
    ]
    run = _run(command, timeout)
    if run.returncode != 0:
        for path in dest.glob(f"youtube-{video_id}.*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        raise YouTubeImportError("Не удалось скачать публичное видео YouTube")
    candidates = [p for p in dest.glob(f"youtube-{video_id}.*")
                  if p.is_file() and p.suffix not in {".part", ".ytdl"}]
    if len(candidates) != 1:
        for path in candidates:
            path.unlink(missing_ok=True)
        raise YouTubeImportError("YouTube-загрузка не создала один видеофайл")
    path = candidates[0]
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        path.unlink(missing_ok=True)
        raise YouTubeImportError("Видео превышает разрешённый размер")
    title = str(meta.get("title") or f"YouTube {video_id}").strip()[:300]
    return YouTubeDownload(str(path), video_id, title, duration, url, size)
