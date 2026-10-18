#!/usr/bin/env python3
"""Клиент админки «База знаний» AgroBot — реальный контракт авторизации.

Заголовок: Authorization: tma <URL-encoded initData> — ровно как это
делает настоящий Mini App (`encodeURIComponent`; см. agrobot_tma.py).

Контракт загрузки (routes/admin-knowledge/index.ts):
  POST {base}/api/admin/knowledge/documents
  multipart/form-data: files (не более 5 за запрос — MAX_FILES_PER_UPLOAD
  на сервере), category (обязательно, 1-120 символов), comment
  (опционально), force=1 (опционально).
  Ответ: 201 при accepted>0, иначе 200; тело
  {success: bool, data: {results: [{ok, name, code?, documentId?,
  queued?, error?, duplicateOf?}], accepted: N}} — структурный ответ
  с per-file результатами приходит ЦЕЛИКОМ даже при accepted=0.

Живость сервера: GET {base}/api/healthz — реальный маршрут AgroBot
(app.ts: app.use('/api', router) + routes/health.ts router.get('/healthz')).

Батчинг (ревью pass3, P2): список файлов режется на запросы — не больше
AGROBOT_UPLOAD_BATCH_MAX_FILES (по умолчанию 5, как серверный лимит)
и не больше AGROBOT_UPLOAD_BATCH_MAX_BYTES (по умолчанию 16 МиБ)
сырых байт файлов на запрос; тело одного запроса собирается в памяти
только для своего батча — «гигабайтного» тела из всех файлов сразу
больше не возникает. Один файл крупнее лимита уходит отдельным
запросом (окончательное решение остаётся за сервером: 20 МиБ/файл).
У сервера также рейт-лимит ~10 запросов/мин на upload — клиент шлёт
батчи последовательно, без параллелизма.

initData — кратковременный документ (TMA_AUTH_MAX_AGE_SECONDS, по умолчанию
24 ч на сервере), НЕ сервисный токен: получайте свежую сессию перед
публикацией; клиент предупреждает о возрасте (warn_after_seconds).
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agrobot_tma import (encode_init_data_header,  # noqa: E402
                         init_data_age_seconds)

DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_WARN_AFTER_SECONDS = 12 * 60 * 60  # «осталось меньше половины срока»
#: серверные лимиты админки: 5 файлов/запрос (MAX_FILES_PER_UPLOAD),
#: 20 МиБ/файл; батч-лимит байт выбираем с запасом ниже 5×20 МиБ
DEFAULT_BATCH_MAX_FILES = 5
DEFAULT_BATCH_MAX_BYTES = 16 * 1024 * 1024


class AgrobotClientError(RuntimeError):
    def __init__(self, status: int, message: str, payload: dict | None = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.payload = payload


def _positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"{name}: ожидалось целое число, получено "
                         f"{value!r}") from None
    if parsed < 1:
        raise ValueError(f"{name}: ожидалось >= 1, получено {parsed}")
    return parsed


class AgrobotClient:
    """Минимальный клиент admin API (stdlib only, без кода/ключей AgroBot)."""

    def __init__(self, base_url: str, init_data: str, *,
                 timeout: float = 60.0,
                 max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
                 warn_after_seconds: float = DEFAULT_WARN_AFTER_SECONDS,
                 batch_max_files: int | None = None,
                 batch_max_bytes: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.init_data = init_data
        self.timeout = timeout
        self.max_age_seconds = max_age_seconds
        self.warn_after_seconds = warn_after_seconds
        self.batch_max_files = (batch_max_files if batch_max_files is not None
                                else _positive_int_env(
                                    "AGROBOT_UPLOAD_BATCH_MAX_FILES",
                                    DEFAULT_BATCH_MAX_FILES))
        self.batch_max_bytes = (batch_max_bytes if batch_max_bytes is not None
                                else _positive_int_env(
                                    "AGROBOT_UPLOAD_BATCH_MAX_BYTES",
                                    DEFAULT_BATCH_MAX_BYTES))

    def check_init_data_fresh(self) -> float:
        """Пред-проверка возраста initData перед запросами.

        Возвращает возраст в секундах. Кидает ошибку, если данные уже
        просрочены (сервер всё равно отвергнет с 401); печатает
        предупреждение, если срок на исходе.
        """
        age = init_data_age_seconds(self.init_data)
        if age > self.max_age_seconds:
            raise AgrobotClientError(
                401, "initData просрочено: получите свежую сессию Mini App "
                     "(initData — не постоянный сервисный токен)")
        if age > self.warn_after_seconds:
            print(f"ВНИМАНИЕ: initData возраст {age/3600:.1f} ч — скоро "
                  "истечёт; лучше получить свежую сессию", file=sys.stderr)
        return age

    def _headers(self) -> dict[str, str]:
        return {"Authorization": encode_init_data_header(self.init_data)}

    def _request(self, path: str, *, method: str = "GET",
                 data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(self.base_url + path, data=data,
                                         method=method)
        for key, value in {**self._headers(), **(headers or {})}.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            status = exc.code
        except urllib.error.URLError as exc:
            raise AgrobotClientError(
                0, f"нет соединения с AgroBot ({exc.reason})") from exc
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"raw": body}
        return status, payload

    def healthz(self) -> dict:
        """Живость сервера — реальный маршрут AgroBot /api/healthz."""
        status, payload = self._request("/api/healthz")
        return payload if status == 200 else {}

    def upload_documents(self, files: list[Path], *, category: str,
                         comment: str | None = None,
                         force: bool = False,
                         continue_on_error: bool = False) -> dict:
        """Загрузка документов; возвращает data:{results, accepted}.

        Файлы режутся на батчи (≤ batch_max_files и ≤ batch_max_bytes
        на запрос — сервер принимает не больше 5 файлов). При
        continue_on_error=True отказавший батч не прерывает остальные:
        его файлы попадают в results с ok=False (code/error), и метод
        возвращает агрегат по всем батчам; иначе первый отказ — исключение
        AgrobotClientError, как прежде.

        Ответ парсится по РЕАЛЬНОМУ контракту (в т.ч. 200 при accepted=0 —
        карточки дубликатов/конфликтов приходят целиком).
        """
        if not files:
            raise ValueError("files list is empty")
        if not (1 <= len(category.strip()) <= 120):
            raise ValueError("category must be 1-120 chars")
        results: list[dict] = []
        for batch in _batch_files(files, max_files=self.batch_max_files,
                                  max_bytes=self.batch_max_bytes):
            boundary, body = _multipart_files(
                batch, category=category, comment=comment, force=force)
            if continue_on_error:
                try:
                    results.extend(self._upload_batch(boundary, body, batch))
                except AgrobotClientError as exc:
                    results.extend(
                        {"ok": False, "name": path.name,
                         "code": "upload_failed",
                         "error": str(exc)} for path in batch)
            else:
                results.extend(self._upload_batch(boundary, body, batch))
        accepted = sum(1 for r in results if r.get("ok"))
        return {"results": results, "accepted": accepted}

    def _upload_batch(self, boundary: str, body: bytes,
                      batch: list[Path]) -> list[dict]:
        status, payload = self._request(
            "/api/admin/knowledge/documents", method="POST", data=body,
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        if status not in (200, 201):
            message = (payload.get("error") if isinstance(payload, dict)
                       else None) or "upload failed"
            raise AgrobotClientError(status, str(message), payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "results" not in data:
            raise AgrobotClientError(
                status, "unexpected upload response shape", payload)
        return list(data["results"])


def _batch_files(files: list[Path], *, max_files: int,
                 max_bytes: int) -> list[list[Path]]:
    """Группировка файлов в батчи: ≤max_files и ≤max_bytes на запрос.

    Файл крупнее max_bytes идёт отдельным батчом (решение о лимите
    остаётся за сервером). Возвращает список непустых батчей.
    """
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_bytes = 0
    for path in files:
        size = path.stat().st_size
        if current and (len(current) >= max_files
                        or current_bytes + size > max_bytes):
            batches.append(current)
            current, current_bytes = [], 0
        if size > max_bytes and not current:
            batches.append([path])       # одиночный «сверхбольшой» батч
            continue
        current.append(path)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def _multipart_files(files: list[Path], *, category: str,
                     comment: str | None, force: bool
                     ) -> tuple[str, bytes]:
    boundary = "----agrobot-" + uuid.uuid4().hex

    def field(name: str, value: str) -> bytes:
        return (f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")

    parts = [field("category", category)]
    if comment:
        parts.append(field("comment", comment))
    if force:
        parts.append(field("force", "1"))
    for path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head = (f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"files\"; "
                f"filename=\"{path.name}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        with open(path, "rb") as handle:
            content = handle.read()
        parts.append(head + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(parts)


if __name__ == "__main__":  # мини-смоук: мок + подписанная демо-initData
    import subprocess
    mock = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "mock_agrobot.py"),
         "8098"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    try:
        from agrobot_tma import make_demo_init_data
        client = AgrobotClient("http://127.0.0.1:8098",
                               make_demo_init_data("mock-bot-token"))
        print("age:", round(client.check_init_data_fresh()), "s")
        print("healthz:", client.healthz())
        data = client.upload_documents(
            [Path(__file__)], category="Смоук-категория")
        print("accepted:", data["accepted"], "results:",
              json.dumps(data["results"], ensure_ascii=False)[:200])
    finally:
        mock.terminate()
