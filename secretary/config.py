"""Конфигурация Secretary Core из переменных окружения.

Правила:
- секрет (ключ прокси) никогда не попадает в repr/log (метод redacted);
- base URL без /v1, без userinfo (логин:пароль@хост запрещён);
- HTTPS обязателен для удалённых хостов; http допускается только для
  loopback (127.0.0.1, ::1, localhost) при allow_loopback_http;
- все числовые лимиты положительны и валидируются при загрузке.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import ConfigError

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
DEFAULT_SEGMENT_SECONDS = 900.0  # 15 минут — преемственность с docs/HISTORY.md
DEFAULT_OVERLAP_SECONDS = 4.0
DEFAULT_MIN_SEGMENT_SECONDS = 30.0
DEFAULT_MAX_AUDIO_BYTES = 10_485_760  # лимит одного сегмента для прокси


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: str, name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if number <= 0:
        raise ConfigError(f"{name} must be positive, got {number}")
    return number


def _parse_int(value: str, name: str) -> int:
    number = _parse_float(value, name)
    if number != int(number):
        raise ConfigError(f"{name} must be an integer, got {number}")
    return int(number)


@dataclass(frozen=True)
class SecretaryConfig:
    proxy_base_url: str = ""
    proxy_api_key: str = ""
    model: str = ""
    auth_header: str = "x-api-key"  # "x-api-key" | "authorization"
    max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES  # сегмент после конвертации
    max_source_bytes: int = 1_073_741_824  # исходный файл, 1 ГиБ
    max_duration_seconds: float = 43_200.0  # 12 часов на запись
    concurrency: int = 1  # тяжёлый воркер последовательный по умолчанию
    segment_seconds: float = DEFAULT_SEGMENT_SECONDS
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS
    min_segment_seconds: float = DEFAULT_MIN_SEGMENT_SECONDS
    request_timeout_seconds: float = 300.0
    max_attempts: int = 4
    backoff_base_seconds: float = 1.5
    backoff_max_seconds: float = 30.0
    retry_after_cap_seconds: float = 120.0
    db_path: str = "data/secretary.db"
    jobs_dir: str = "data/jobs"
    api_token: str = ""
    allow_loopback_http: bool = True
    keep_segment_files: bool = False
    structure_chunk_chars: int = 50_000
    disk_budget_bytes: int = 2_147_483_648  # свободное место под задачи, 2 ГиБ
    ffmpeg_timeout_seconds: float = 180.0
    worker_poll_seconds: float = 0.5
    max_zip_entries: int = 200
    max_zip_total_bytes: int = 1_073_741_824
    max_zip_member_bytes: int = 268_435_456
    max_zip_ratio: float = 100.0
    url_max_bytes: int = 536_870_912  # 512 МиБ, потоково, без загрузки в RAM
    url_max_redirects: int = 4
    url_timeout_seconds: float = 120.0
    url_min_free_after_bytes: int = 536_870_912
    video_model: str = ""
    video_max_source_bytes: int = 536_870_912
    video_max_duration_seconds: float = 1_800.0
    video_segment_seconds: float = 120.0
    video_min_segment_seconds: float = 10.0
    video_max_segment_bytes: int = 10_485_760
    video_max_width: int = 960
    video_keep_source: bool = False
    ytdlp_path: str = "yt-dlp"
    youtube_max_duration_seconds: float = 1_800.0
    youtube_max_bytes: int = 536_870_912
    youtube_timeout_seconds: float = 600.0

    def redacted(self) -> dict:
        """Безопасное представление для логов: секреты маскируются."""
        return {
            "proxy_base_url": self.proxy_base_url,
            "model": self.model,
            "auth_header": self.auth_header,
            "proxy_api_key": "***" if self.proxy_api_key else "",
            "api_token": "***" if self.api_token else "",
            "max_audio_bytes": self.max_audio_bytes,
            "segment_seconds": self.segment_seconds,
            "overlap_seconds": self.overlap_seconds,
            "concurrency": self.concurrency,
            "video_model": self.video_model,
            "video_segment_seconds": self.video_segment_seconds,
            "ytdlp_path": self.ytdlp_path,
            "youtube_max_duration_seconds": self.youtube_max_duration_seconds,
        }

    def validate(self) -> "SecretaryConfig":
        if self.auth_header not in {"x-api-key", "authorization"}:
            raise ConfigError("auth_header must be 'x-api-key' or 'authorization'")
        if self.proxy_base_url:
            self._validate_base_url(self.proxy_base_url, self.allow_loopback_http)
        if self.overlap_seconds >= self.min_segment_seconds:
            raise ConfigError("overlap_seconds must be < min_segment_seconds")
        if self.min_segment_seconds >= self.segment_seconds:
            raise ConfigError("min_segment_seconds must be < segment_seconds")
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be >= 1")
        if self.concurrency < 1:
            raise ConfigError("concurrency must be >= 1")
        if self.worker_poll_seconds <= 0:
            raise ConfigError("worker_poll_seconds must be positive")
        if self.max_zip_entries < 1:
            raise ConfigError("max_zip_entries must be >= 1")
        if self.max_zip_ratio < 1.0:
            raise ConfigError("max_zip_ratio must be >= 1")
        if self.max_zip_member_bytes > self.max_zip_total_bytes:
            raise ConfigError(
                "max_zip_member_bytes must not exceed max_zip_total_bytes")
        if self.url_max_redirects < 0:
            raise ConfigError("url_max_redirects must be >= 0")
        if self.video_min_segment_seconds >= self.video_segment_seconds:
            raise ConfigError(
                "video_min_segment_seconds must be < video_segment_seconds")
        if self.video_max_width < 320 or self.video_max_width > 1920:
            raise ConfigError("video_max_width must be in 320..1920")
        return self

    @staticmethod
    def _validate_base_url(url: str, allow_loopback_http: bool) -> None:
        parts = urlsplit(url)
        if parts.username or parts.password:
            raise ConfigError("proxy base URL must not contain userinfo")
        if not parts.hostname:
            raise ConfigError("proxy base URL must contain a host")
        if parts.query or parts.fragment:
            raise ConfigError("proxy base URL must not contain query/fragment")
        loopback = parts.hostname.lower() in LOOPBACK_HOSTS
        if parts.scheme == "http":
            if not (loopback and allow_loopback_http):
                raise ConfigError(
                    "http scheme allowed only for loopback with "
                    "allow_loopback_http; use https for remote proxies")
        elif parts.scheme != "https":
            raise ConfigError("proxy base URL scheme must be http or https")
        if url.rstrip("/").endswith("/v1"):
            raise ConfigError("set base URL without /v1 suffix")

    @classmethod
    def from_env(cls, env: dict | None = None, *,
                 env_file: str | None = None) -> "SecretaryConfig":
        """Собирает конфиг из окружения; env_file — простой KEY=VALUE файл."""
        merged: dict[str, str] = {}
        if env_file and os.path.isfile(env_file):
            merged.update(_read_env_file(env_file))
        merged.update(os.environ if env is None else env)
        get = merged.get

        def f(key: str, default: float) -> float:
            return _parse_float(get(key, str(default)), key) if get(key) else default

        def i(key: str, default: int) -> int:
            return _parse_int(get(key, str(default)), key) if get(key) else default

        cfg = cls(
            proxy_base_url=(get("SECRETARY_PROXY_BASE_URL") or "").strip().rstrip("/"),
            proxy_api_key=(get("SECRETARY_PROXY_API_KEY") or "").strip(),
            model=(get("SECRETARY_MODEL") or "").strip(),
            auth_header=(get("SECRETARY_PROXY_AUTH_HEADER") or "x-api-key").strip(),
            max_audio_bytes=i("SECRETARY_MAX_AUDIO_BYTES", cls.max_audio_bytes),
            max_source_bytes=i("SECRETARY_MAX_SOURCE_BYTES", cls.max_source_bytes),
            max_duration_seconds=f("SECRETARY_MAX_DURATION_SECONDS",
                                    cls.max_duration_seconds),
            concurrency=i("SECRETARY_CONCURRENCY", cls.concurrency),
            segment_seconds=f("SECRETARY_SEGMENT_SECONDS", cls.segment_seconds),
            overlap_seconds=f("SECRETARY_OVERLAP_SECONDS", cls.overlap_seconds),
            min_segment_seconds=f("SECRETARY_MIN_SEGMENT_SECONDS",
                                   cls.min_segment_seconds),
            request_timeout_seconds=f("SECRETARY_REQUEST_TIMEOUT_SECONDS",
                                      cls.request_timeout_seconds),
            max_attempts=i("SECRETARY_MAX_ATTEMPTS", cls.max_attempts),
            backoff_base_seconds=f("SECRETARY_BACKOFF_BASE_SECONDS",
                                   cls.backoff_base_seconds),
            backoff_max_seconds=f("SECRETARY_BACKOFF_MAX_SECONDS",
                                  cls.backoff_max_seconds),
            retry_after_cap_seconds=f("SECRETARY_RETRY_AFTER_CAP_SECONDS",
                                      cls.retry_after_cap_seconds),
            db_path=get("SECRETARY_DB_PATH", cls.db_path),
            jobs_dir=get("SECRETARY_JOBS_DIR", cls.jobs_dir),
            api_token=(get("SECRETARY_API_TOKEN") or "").strip(),
            allow_loopback_http=_parse_bool(
                get("SECRETARY_ALLOW_LOOPBACK_HTTP", "true")),
            keep_segment_files=_parse_bool(
                get("SECRETARY_KEEP_SEGMENT_FILES", "false")),
            structure_chunk_chars=i("SECRETARY_STRUCTURE_CHUNK_CHARS",
                                    cls.structure_chunk_chars),
            disk_budget_bytes=i("SECRETARY_DISK_BUDGET_BYTES", cls.disk_budget_bytes),
            ffmpeg_timeout_seconds=f("SECRETARY_FFMPEG_TIMEOUT_SECONDS",
                                     cls.ffmpeg_timeout_seconds),
            worker_poll_seconds=f("SECRETARY_WORKER_POLL_SECONDS",
                                  cls.worker_poll_seconds),
            max_zip_entries=i("SECRETARY_MAX_ZIP_ENTRIES", cls.max_zip_entries),
            max_zip_total_bytes=i("SECRETARY_MAX_ZIP_TOTAL_BYTES",
                                  cls.max_zip_total_bytes),
            max_zip_member_bytes=i("SECRETARY_MAX_ZIP_MEMBER_BYTES",
                                   cls.max_zip_member_bytes),
            max_zip_ratio=f("SECRETARY_MAX_ZIP_RATIO", cls.max_zip_ratio),
            url_max_bytes=i("SECRETARY_URL_MAX_BYTES", cls.url_max_bytes),
            url_max_redirects=i("SECRETARY_URL_MAX_REDIRECTS",
                                cls.url_max_redirects),
            url_timeout_seconds=f("SECRETARY_URL_TIMEOUT_SECONDS",
                                  cls.url_timeout_seconds),
            url_min_free_after_bytes=i("SECRETARY_URL_MIN_FREE_AFTER_BYTES",
                                       cls.url_min_free_after_bytes),
            video_model=(get("SECRETARY_VIDEO_MODEL") or "").strip(),
            video_max_source_bytes=i("SECRETARY_VIDEO_MAX_SOURCE_BYTES",
                                     cls.video_max_source_bytes),
            video_max_duration_seconds=f(
                "SECRETARY_VIDEO_MAX_DURATION_SECONDS",
                cls.video_max_duration_seconds),
            video_segment_seconds=f("SECRETARY_VIDEO_SEGMENT_SECONDS",
                                    cls.video_segment_seconds),
            video_min_segment_seconds=f("SECRETARY_VIDEO_MIN_SEGMENT_SECONDS",
                                        cls.video_min_segment_seconds),
            video_max_segment_bytes=i("SECRETARY_VIDEO_MAX_SEGMENT_BYTES",
                                      cls.video_max_segment_bytes),
            video_max_width=i("SECRETARY_VIDEO_MAX_WIDTH",
                              cls.video_max_width),
            video_keep_source=_parse_bool(
                get("SECRETARY_VIDEO_KEEP_SOURCE", "false")),
            ytdlp_path=(get("SECRETARY_YTDLP_PATH") or "yt-dlp").strip(),
            youtube_max_duration_seconds=f(
                "SECRETARY_YOUTUBE_MAX_DURATION_SECONDS",
                cls.youtube_max_duration_seconds),
            youtube_max_bytes=i("SECRETARY_YOUTUBE_MAX_BYTES",
                                cls.youtube_max_bytes),
            youtube_timeout_seconds=f("SECRETARY_YOUTUBE_TIMEOUT_SECONDS",
                                      cls.youtube_timeout_seconds),
        )
        return cfg.validate()


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values
