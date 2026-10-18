"""Небольшой документированный HTTP API + тяжёлый воркер (stdlib only).

Маршруты (см. docs/INTEGRATION.md):
  GET  /healthz
  GET  /v1/jobs?status=&limit=
  POST /v1/jobs                      (тело: аудио-байты; заголовки ниже)
  POST /v1/url-jobs                  (JSON: публичная HTTPS/Drive-ссылка)
  GET  /v1/jobs/{id}                 прогресс
  GET  /v1/jobs/{id}/result          JSON v1 (терминальные статусы)
  GET  /v1/jobs/{id}/markdown        Markdown (терминальные статусы)
  POST /v1/jobs/{id}/cancel
  POST /v1/jobs/{id}/retry-failed

Заголовки создания: X-Secretary-Filename (обязателен), X-Secretary-Language,
X-Secretary-Vocabulary-B64 (base64 JSON-массив UTF-8; старый ASCII-заголовок
X-Secretary-Vocabulary поддерживается для совместимости), X-Secretary-Owner.
Авторизация сервера: Bearer SECRETARY_API_TOKEN, если задан.

Загрузка — ПОТОКОВАЯ: тело запроса пишется на диск кусками с подсчётом
байт, в оперативной памяти не держится ни исходник, ни его копия
(целевая VM ~1 ГБ RAM). Требуется Content-Length; chunked не поддерживается
(считается 411). Превышение лимита — отказ до дочитывания тела.

Bind не на loopback БЕЗ обязательного SECRETARY_API_TOKEN запрещён
(raise на старте). Сравнение токена — constant-time. Внутренние ошибки
(500) не раскрывают деталей клиенту: подробности только в логе сервера.

Воркер: один фоновый поток, последовательно забирает queued-задачи
(concurrency=1 по умолчанию); отмена проверяется между сегментами и
между попытками транспорта.
"""
from __future__ import annotations

import hmac
import base64
import binascii
import json
import logging
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .engine import TranscriptionEngine
from .errors import (AudioError, ConfigError, NotFoundError, SecretaryError,
                     UrlImportError, ValidationError, VideoError,
                     YouTubeImportError)
from .result import build_result, render_markdown
from .urlimport import download_url
from .youtube import download_youtube

log = logging.getLogger("secretary.server")

_TERMINAL = {"completed", "partial", "failed", "cancelled"}
_UPLOAD_CHUNK = 1024 * 1024  # 1 МиB: тело пишется на диск кусками
_URL_JOB_JSON_LIMIT = 16 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Worker:
    def __init__(self, engine: TranscriptionEngine, *, poll_seconds: float):
        self.engine = engine
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="secretary-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def busy(self) -> bool:
        return any(job["status"] == "running"
                   for job in self.engine.store.list_jobs(limit=1000))

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.engine.store.claim_next_job()
            if job is None:
                self._stop.wait(self.poll_seconds)
                continue
            try:
                self.engine.run_job(job["id"])
            except Exception:  # noqa: BLE001
                log.exception("worker crashed on job %s", job["id"])
                self.engine.store.reset_running_segments(job["id"])
                self.engine.store.update_job(job["id"], status="queued")
                self._stop.wait(self.poll_seconds)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SecretaryCore/1"
    protocol_version = "HTTP/1.1"
    engine: TranscriptionEngine
    api_token: str

    def log_message(self, fmt: str, *args):  # секреты в URL не попадают
        log.info("%s %s", self.address_string(), fmt % args)

    # ---------------------------------------------------------------- helpers
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, type_name: str, message: str) -> None:
        self._send_json(status, {"error": {"type": type_name, "message": message}})

    def _authorized(self) -> bool:
        if not self.api_token:
            return True
        header = self.headers.get("Authorization", "")
        # constant-time сравнение (см. ревью pass2, P2)
        return hmac.compare_digest(header.encode("utf-8"),
                                   f"Bearer {self.api_token}".encode("utf-8"))

    def _stream_body_to_file(self, limit: int, suffix: str = "") -> Path:
        """Потоковая запись тела запроса во временный файл с лимитом.

        Тело НЕ читается в оперативную память: кусками по _UPLOAD_CHUNK
        пишется в temp-файл рядом с jobs_dir (та же ФС — перенос без копии,
        см. engine.create_job(move_source=True)). Суффикс temp-файла =
        расширению загружаемого имени — валидация аудио по расширению и
        сигнатуре работает как для обычных файлов. Превышение лимита —
        ValidationError до дочитывания тела; частичный файл удаляется.
        """
        if (self.headers.get("Transfer-Encoding", "").lower() or "").strip() \
                == "chunked":
            # тело не читаем — закрываем соединение после ответа
            self.close_connection = True
            raise ValidationError(
                "chunked transfer is not supported; send Content-Length")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length header") from exc
        if length <= 0:
            raise ValidationError("empty request body")
        if length > limit:
            # тело не дочитываем — закрываем соединение после ответа
            self.close_connection = True
            raise ValidationError(
                f"body exceeds limit ({length} > {limit} bytes)")
        jobs_root = Path(self.engine.config.jobs_dir)
        jobs_root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="upload-",
                                        suffix=suffix or ".part",
                                        dir=str(jobs_root))
        tmp_path = Path(tmp_name)
        remaining = length
        try:
            with os.fdopen(fd, "wb") as dst:
                while remaining > 0:
                    chunk = self.rfile.read(min(_UPLOAD_CHUNK, remaining))
                    if not chunk:
                        raise ValidationError(
                            "connection closed before body was fully received")
                    dst.write(chunk)
                    remaining -= len(chunk)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return tmp_path

    def _result(self, job_id: str) -> dict:
        job = self.engine.store.get_job(job_id)
        return build_result(job, self.engine.store.list_segments(job_id))

    def _assert_expected_owner(self, job_id: str) -> None:
        """Optional defense-in-depth for trusted server-side adapters.

        Old CLI/API clients remain compatible when the header is absent. The
        AgroBot adapter always sends it, so one knowledge administrator cannot
        read another administrator's transcription by presenting a job id.
        Mismatch deliberately looks like a missing job.
        """
        expected = self.headers.get("X-Secretary-Expected-Owner")
        if expected is None:
            return
        job = self.engine.store.get_job(job_id)
        if not hmac.compare_digest(
                str(job.get("owner_id", "")).encode("utf-8"),
                expected.encode("utf-8")):
            raise NotFoundError("job not found")

    def _read_small_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length header") from exc
        if length <= 0 or length > _URL_JOB_JSON_LIMIT:
            raise ValidationError("JSON body must be 1..16384 bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValidationError("JSON body must be an object")
        return payload

    def _internal_error(self, exc: Exception) -> None:
        """500 без деталей клиенту: подробности только в логе сервера."""
        log.exception("request failed on %s", self.path)
        self._error(500, "internal", "internal server error")

    # ---------------------------------------------------------------- routes
    def do_GET(self):  # noqa: N802
        try:
            if not self._authorized():
                self._error(401, "unauthorized", "missing or invalid token")
                return
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send_json(200, {"ok": True, "worker": "running"})
                return
            if path == "/v1/jobs":
                query = parse_qs(urlsplit(self.path).query)
                status = (query.get("status") or [None])[0]
                limit = int((query.get("limit") or ["50"])[0])
                jobs = self.engine.store.list_jobs(status=status, limit=limit)
                self._send_json(200, {"jobs": [
                    {"job_id": j["id"], "status": j["status"],
                     "media_kind": j.get("media_kind") or "audio",
                     "filename": j["source_filename"],
                     "created_at": j["created_at"]} for j in jobs]})
                return
            if path.startswith("/v1/jobs/"):
                parts = [p for p in path.split("/") if p]
                job_id = parts[2] if len(parts) >= 3 else ""
                self._assert_expected_owner(job_id)
                if len(parts) == 3:
                    self._send_json(200, self.engine.progress(job_id))
                    return
                if len(parts) == 4 and parts[3] == "result":
                    result = self._result(job_id)
                    if result["status"] not in _TERMINAL:
                        self._error(409, "not_finished",
                                    "job is still in progress")
                        return
                    self._send_json(200, result)
                    return
                if len(parts) == 4 and parts[3] == "markdown":
                    result = self._result(job_id)
                    if result["status"] not in _TERMINAL:
                        self._error(409, "not_finished",
                                    "job is still in progress")
                        return
                    body = render_markdown(result).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/markdown; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self._error(404, "not_found", "unknown route")
        except NotFoundError:
            self._error(404, "not_found", "job not found")
        except AudioError as exc:
            self._error(400, "unsupported_audio", str(exc))
        except (ValidationError, ValueError) as exc:
            self._error(400, "bad_request", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._internal_error(exc)

    def do_POST(self):  # noqa: N802
        try:
            if not self._authorized():
                self._error(401, "unauthorized", "missing or invalid token")
                return
            path = urlsplit(self.path).path
            if path == "/v1/jobs":
                self._create_job()
                return
            if path == "/v1/video-jobs":
                self._create_video_job()
                return
            if path == "/v1/url-jobs":
                self._create_url_job(video_mode=False)
                return
            if path == "/v1/video-url-jobs":
                self._create_url_job(video_mode=True)
                return
            if path == "/v1/youtube-jobs":
                self._create_youtube_job()
                return
            if path.startswith("/v1/jobs/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 4 and parts[3] == "cancel":
                    status = self.engine.cancel(parts[2])
                    self._send_json(200, {"job_id": parts[2], "status": status})
                    return
                if len(parts) == 4 and parts[3] == "retry-failed":
                    self._send_json(200, self.engine.retry_failed(parts[2]))
                    return
            self._error(404, "not_found", "unknown route")
        except NotFoundError:
            self._error(404, "not_found", "job not found")
        except AudioError as exc:
            self._error(400, "unsupported_audio", str(exc))
        except VideoError as exc:
            self._error(400, "unsupported_video", str(exc))
        except UrlImportError as exc:
            self._error(400, "url_import", str(exc))
        except YouTubeImportError as exc:
            self._error(400, "youtube_import", str(exc))
        except ValidationError as exc:
            self._error(400, "bad_request", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._internal_error(exc)

    def _create_job(self) -> None:
        filename = self.headers.get("X-Secretary-Filename")
        filename_b64 = self.headers.get("X-Secretary-Filename-B64")
        if filename_b64:
            try:
                filename = base64.b64decode(
                    filename_b64.encode("ascii"), validate=True
                ).decode("utf-8")
            except (UnicodeError, ValueError, binascii.Error) as exc:
                raise ValidationError(
                    "X-Secretary-Filename-B64 must be valid base64 UTF-8"
                ) from exc
        if not filename:
            raise ValidationError("X-Secretary-Filename header is required")
        filename = filename.strip()
        if len(filename) > 255 or "/" in filename or "\\" in filename \
                or filename.startswith("."):
            raise ValidationError("unsafe filename")
        language = self.headers.get("X-Secretary-Language") or None
        vocabulary_b64 = self.headers.get("X-Secretary-Vocabulary-B64")
        if vocabulary_b64:
            try:
                vocabulary_json = base64.b64decode(
                    vocabulary_b64.encode("ascii"), validate=True
                ).decode("utf-8")
                vocabulary = json.loads(vocabulary_json)
            except (UnicodeError, ValueError, binascii.Error,
                    json.JSONDecodeError) as exc:
                raise ValidationError(
                    "X-Secretary-Vocabulary-B64 must be base64 UTF-8 JSON"
                ) from exc
            if (not isinstance(vocabulary, list)
                    or any(not isinstance(item, str) for item in vocabulary)
                    or len(vocabulary) > 200):
                raise ValidationError(
                    "X-Secretary-Vocabulary-B64 must contain a string list"
                )
            vocabulary = [v.strip() for v in vocabulary if v.strip()]
        else:
            vocabulary_raw = self.headers.get("X-Secretary-Vocabulary") or ""
            vocabulary = [v.strip() for v in vocabulary_raw.split(",")
                          if v.strip()]
        owner = self.headers.get("X-Secretary-Owner") or "local"
        suffix = Path(filename).suffix.lower()
        # Потоковая загрузка: тело сразу на диск, в RAM не держится.
        # Лимит проверяется по Content-Length до чтения и по факту при записи.
        # Суффикс temp-файла = расширению имени — MIME-валидация корректна.
        tmp_path = self._stream_body_to_file(
            self.engine.config.max_source_bytes, suffix=suffix)
        if tmp_path.stat().st_size > self.engine.config.max_source_bytes:
            tmp_path.unlink(missing_ok=True)
            raise ValidationError("body exceeds limit on actual write")
        try:
            # move_source=True: сервер владеет temp-файлом, перенос в job-директорию
            # без полной копии (внутри — os.replace, при EXDEV — copy+unlink).
            created = self.engine.create_job(
                tmp_path, filename=filename, owner_id=owner,
                language=language, vocabulary=vocabulary,
                move_source=True)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        status_code = 201 if created["created"] else 200
        self._send_json(status_code, created)

    def _create_video_job(self) -> None:
        filename, language, vocabulary, owner = self._creation_headers()
        suffix = Path(filename).suffix.lower()
        limit = self.engine.config.video_max_source_bytes
        tmp_path = self._stream_body_to_file(limit, suffix=suffix)
        try:
            created = self.engine.create_video_job(
                tmp_path, filename=filename, owner_id=owner,
                language=language, vocabulary=vocabulary, move_source=True)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        self._send_json(201 if created["created"] else 200, created)

    def _creation_headers(self) -> tuple[str, str | None, list[str], str]:
        filename = self.headers.get("X-Secretary-Filename")
        filename_b64 = self.headers.get("X-Secretary-Filename-B64")
        if filename_b64:
            try:
                filename = base64.b64decode(
                    filename_b64.encode("ascii"), validate=True).decode("utf-8")
            except (UnicodeError, ValueError, binascii.Error) as exc:
                raise ValidationError(
                    "X-Secretary-Filename-B64 must be valid base64 UTF-8") from exc
        if not filename:
            raise ValidationError("X-Secretary-Filename header is required")
        filename = filename.strip()
        if len(filename) > 255 or "/" in filename or "\\" in filename \
                or filename.startswith("."):
            raise ValidationError("unsafe filename")
        language = self.headers.get("X-Secretary-Language") or None
        vocabulary_b64 = self.headers.get("X-Secretary-Vocabulary-B64")
        if vocabulary_b64:
            try:
                vocabulary = json.loads(base64.b64decode(
                    vocabulary_b64.encode("ascii"), validate=True).decode("utf-8"))
            except (UnicodeError, ValueError, binascii.Error,
                    json.JSONDecodeError) as exc:
                raise ValidationError(
                    "X-Secretary-Vocabulary-B64 must be base64 UTF-8 JSON") from exc
            if (not isinstance(vocabulary, list)
                    or any(not isinstance(item, str) for item in vocabulary)
                    or len(vocabulary) > 200):
                raise ValidationError(
                    "X-Secretary-Vocabulary-B64 must contain a string list")
            vocabulary = [v.strip() for v in vocabulary if v.strip()]
        else:
            vocabulary = [v.strip() for v in
                          (self.headers.get("X-Secretary-Vocabulary") or "").split(",")
                          if v.strip()]
        return filename, language, vocabulary, \
            self.headers.get("X-Secretary-Owner") or "local"

    def _create_url_job(self, *, video_mode: bool) -> None:
        payload = self._read_small_json()
        url = payload.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValidationError("url is required")
        owner = payload.get("owner", "local")
        language = payload.get("language")
        vocabulary = payload.get("vocabulary", [])
        if not isinstance(owner, str) or len(owner) > 200:
            raise ValidationError("owner must be a short string")
        if language is not None and not isinstance(language, str):
            raise ValidationError("language must be a string")
        if (not isinstance(vocabulary, list)
                or any(not isinstance(item, str) for item in vocabulary)
                or len(vocabulary) > 200):
            raise ValidationError("vocabulary must be a list of strings")
        inbox = Path(self.engine.config.jobs_dir) / ".url-inbox"
        result = download_url(
            url, inbox,
            max_bytes=min(self.engine.config.url_max_bytes,
                          (self.engine.config.video_max_source_bytes
                           if video_mode else self.engine.config.max_source_bytes)),
            max_redirects=self.engine.config.url_max_redirects,
            timeout=self.engine.config.url_timeout_seconds,
            min_free_after_bytes=self.engine.config.url_min_free_after_bytes)
        path = Path(result.path)
        try:
            create = (self.engine.create_video_job if video_mode
                      else self.engine.create_job)
            created = create(
                path, filename=result.filename, owner_id=owner,
                language=language,
                vocabulary=[item.strip() for item in vocabulary if item.strip()],
                move_source=True)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        created["download"] = {
            "filename": result.filename,
            "size": result.size,
            "sha256": result.sha256,
            "content_type": result.content_type,
            "source_host": result.source_host,
        }
        self._send_json(201 if created["created"] else 200, created)

    def _create_youtube_job(self) -> None:
        payload = self._read_small_json()
        url = payload.get("url")
        owner = payload.get("owner", "local")
        language = payload.get("language")
        vocabulary = payload.get("vocabulary", [])
        if not isinstance(url, str) or not url.strip():
            raise ValidationError("url is required")
        if not isinstance(owner, str) or len(owner) > 200:
            raise ValidationError("owner must be a short string")
        if language is not None and not isinstance(language, str):
            raise ValidationError("language must be a string")
        if (not isinstance(vocabulary, list)
                or any(not isinstance(item, str) for item in vocabulary)
                or len(vocabulary) > 200):
            raise ValidationError("vocabulary must be a list of strings")
        inbox = Path(self.engine.config.jobs_dir) / ".youtube-inbox"
        item = download_youtube(
            url, inbox, executable=self.engine.config.ytdlp_path,
            max_duration=min(self.engine.config.youtube_max_duration_seconds,
                             self.engine.config.video_max_duration_seconds),
            max_bytes=min(self.engine.config.youtube_max_bytes,
                          self.engine.config.video_max_source_bytes),
            timeout=self.engine.config.youtube_timeout_seconds)
        path = Path(item.path)
        try:
            created = self.engine.create_video_job(
                path, filename=f"{item.title}{path.suffix.lower()}", owner_id=owner,
                language=language,
                vocabulary=[v.strip() for v in vocabulary if v.strip()],
                move_source=True, source_url=item.source_url)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        created["youtube"] = {
            "video_id": item.video_id, "title": item.title,
            "duration": item.duration, "source_url": item.source_url,
            "size": item.size,
        }
        self._send_json(201 if created["created"] else 200, created)


def make_server(engine: TranscriptionEngine, *, host: str, port: int,
                api_token: str = "") -> tuple[ThreadingHTTPServer, Worker]:
    # Bind не на loopback без токена запрещён (ревью pass2, P2): открытый
    # интерфейс без авторизации — не допустимая конфигурация.
    if host not in _LOOPBACK_HOSTS and not api_token:
        raise ConfigError(
            "SECRETARY_API_TOKEN is required when binding to non-loopback "
            f"host {host!r}")
    handler = type("BoundHandler", (_Handler,),
                   {"engine": engine, "api_token": api_token})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    worker = Worker(engine, poll_seconds=engine.config.worker_poll_seconds)
    return server, worker


def serve(engine: TranscriptionEngine, *, host: str, port: int,
          api_token: str = "") -> None:
    server, worker = make_server(engine, host=host, port=port,
                                 api_token=api_token)
    worker.start()
    log.info("secretary http api listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupt received: shutting down")
    finally:
        worker.stop()
        server.server_close()
