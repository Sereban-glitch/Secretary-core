#!/usr/bin/env python3
"""Пример подключения Secretary Core к AgroBot (без кода и ключей AgroBot).

Флоу «расшифровка → проверка человеком → публикация»:

  1. создать задание расшифровки аудио (HTTP API Secretary);
  2. дождаться завершения, забрать результат (JSON + Markdown);
  3. ПОКАЗАТЬ РЕЗУЛЬТАТ ЧЕЛОВЕКУ;
  4. ТОЛЬКО после явного подтверждения опубликовать расшифровку в AgroBot
     как документ (multipart POST /api/admin/knowledge/documents,
     Authorization: tma <URL-encoded initData> — как настоящий Mini App,
     см. agrobot_tma.encode_init_data_header).

Публикация по умолчанию НЕ выполняется: без интерактивного «да» или флага
--yes адаптер останавливается на шаге 3. Никакие секреты не хранятся:
адреса и токены приходят из окружения.

Для полного приёмочного сценария (ZIP с документами и аудио) используйте
agrobot_orchestrator.py — там потоки документов и аудио разделены явно.

Демо (полностью офлайн, сервер Secretary с --fake-transport):

  терминал 1: python3 -m secretary.cli serve --fake-transport --port 8080
  терминал 2: python3 examples/mock_agrobot.py
  терминал 3: python3 examples/agrobot_adapter.py --demo --yes

Переменные окружения:
  SECRETARY_URL      — базовый URL Secretary (по умолчанию http://127.0.0.1:8080)
  SECRETARY_API_TOKEN — Bearer-токен Secretary, если задан
  AGROBOT_BASE_URL   — базовый URL AgroBot (в демо — мок)
  AGROBOT_INITDATA   — строка Telegram initData администратора (НЕ логируется);
                        кратковременная (≈ 24 ч), НЕ сервисный токен —
                        перед публикацией адаптер проверяет возраст
"""
from __future__ import annotations

import argparse
import json
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

DEMO_AGROBOT = "http://127.0.0.1:8099"
TERMINAL = {"completed", "partial", "failed", "cancelled"}
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60


class SecretaryClient:
    """Минимальный клиент HTTP API Secretary Core (stdlib only)."""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str, *, method: str = "GET", body: bytes = b"",
                 headers: dict | None = None) -> tuple[int, dict | str]:
        request = urllib.request.Request(self.base_url + path, data=body,
                                         method=method)
        request.add_header("Content-Type", "application/octet-stream")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return response.status, payload
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def create_job(self, audio: Path, *, filename: str, language: str | None,
                   vocabulary: list[str], owner: str) -> dict:
        headers = {"X-Secretary-Filename": filename,
                   "X-Secretary-Owner": owner}
        if language:
            headers["X-Secretary-Language"] = language
        if vocabulary:
            headers["X-Secretary-Vocabulary"] = ",".join(vocabulary)
        status, payload = self._request("/v1/jobs", method="POST",
                                        body=audio.read_bytes(),
                                        headers=headers)
        return self._json(status, payload)

    def wait_terminal(self, job_id: str, *, timeout: float = 3600.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, payload = self._request(f"/v1/jobs/{job_id}")
            progress = self._json(status, payload)
            print(f"  статус: {progress['status']} "
                  f"сегменты: {progress['segments']}", flush=True)
            if progress["status"] in TERMINAL:
                return progress
            time.sleep(1.0)
        raise TimeoutError(f"job {job_id} not finished in {timeout}s")

    def result(self, job_id: str) -> dict:
        status, payload = self._request(f"/v1/jobs/{job_id}/result")
        return self._json(status, payload)

    def markdown(self, job_id: str) -> str:
        status, payload = self._request(f"/v1/jobs/{job_id}/markdown")
        if status != 200:
            raise RuntimeError(f"markdown failed: {status} {payload[:200]}")
        return payload

    @staticmethod
    def _json(status: int, payload: str) -> dict:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON ({status})") from exc
        # у результата v1 есть легитимное поле error: null — ошибкой
        # считается только непустой объект error
        if isinstance(data, dict) and data.get("error"):
            message = data["error"].get("message") if isinstance(
                data["error"], dict) else data["error"]
            raise RuntimeError(f"API error ({status}): {message}")
        return data


def build_multipart(field: str, filename: str, content: bytes,
                    content_type: str) -> tuple[str, bytes]:
    """multipart/form-data без внешних зависимостей."""
    boundary = "----secretary-" + uuid.uuid4().hex
    head = (f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return boundary, head + content + tail


def publish_to_agrobot(base_url: str, initdata: str, markdown: str,
                       *, filename: str, category: str = "Расшифровки",
                       comment: str | None = None) -> dict:
    """Публикация расшифровки как документа AgroBot (после подтверждения!).

    Реальный контракт: POST {base}/api/admin/knowledge/documents,
    multipart-поля files + category (+ comment), заголовок
    Authorization: tma <URL-encoded initData> — байт в байт как клиент
    Mini App (encodeURIComponent; см. agrobot_tma.py).
    """
    # единый multipart: files + category (как у реального сервера)
    full_boundary = "----secretary-" + uuid.uuid4().hex
    head_files = (f"--{full_boundary}\r\n"
                  f"Content-Disposition: form-data; name=\"files\"; "
                  f"filename=\"{filename}\"\r\n"
                  f"Content-Type: text/markdown\r\n\r\n").encode("utf-8")
    parts = [head_files + markdown.encode("utf-8") + b"\r\n"]
    parts.append((f"--{full_boundary}\r\n"
                  f"Content-Disposition: form-data; name=\"category\"\r\n"
                  f"\r\n{category}\r\n").encode("utf-8"))
    if comment:
        parts.append((f"--{full_boundary}\r\n"
                      f"Content-Disposition: form-data; name=\"comment\"\r\n"
                      f"\r\n{comment}\r\n").encode("utf-8"))
    parts.append(f"--{full_boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/admin/knowledge/documents",
        data=body, method="POST")
    request.add_header("Content-Type",
                       f"multipart/form-data; boundary={full_boundary}")
    # URL-encoded initData — РЕАЛЬНЫЙ формат авторизации админки
    request.add_header("Authorization", encode_init_data_header(initdata))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return {"status": response.status,
                    "body": response.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code,
                "body": exc.read().decode("utf-8", "replace")}


def human_confirm(result: dict, markdown: str, *, yes: bool) -> bool:
    """Показать результат человеку и запросить явное подтверждение."""
    print("\n===== РЕЗУЛЬТАТ ДЛЯ ПРОВЕРКИ ЧЕЛОВЕКОМ =====")
    print(f"job_id: {result['job_id']}")
    print(f"статус: {result['status']}")
    print(f"источник: {result['source']['filename']} "
          f"({result['source']['duration']:.0f} с, "
          f"sha256 {result['source']['sha256'][:12]}…)")
    print(f"предупреждения: {result['warnings'] or 'нет'}")
    draft = result.get("structured_draft") or {}
    print(f"черновик-сводка: {draft.get('summary', '—')}")
    print("---- фрагмент транскрипта ----")
    print("\n".join(markdown.splitlines()[:12]))
    print("=============================================\n")
    if result["status"] != "completed":
        print("статус не completed — публикация запрещена")
        return False
    if yes:
        print("подтверждение получено флагом --yes")
        return True
    if not sys.stdin.isatty():
        print("нет интерактивного терминала и нет --yes — публикация "
              "пропущена (проверка человеком не пройдена)")
        return False
    answer = input("Опубликовать эту расшифровку в базу знаний? "
                   "введите «да»: ").strip().lower()
    return answer in {"да", "yes", "y"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Secretary → (проверка человеком) → AgroBot")
    parser.add_argument("--file", help="аудио-файл (в демо создаётся синус)")
    parser.add_argument("--language", default=None)
    parser.add_argument("--vocabulary", default="",
                        help="термины предметной области через запятую")
    parser.add_argument("--owner", default="agrobot-expert")
    parser.add_argument("--yes", action="store_true",
                        help="явное подтверждение публикации (для демо)")
    parser.add_argument("--category", default="Расшифровки",
                        help="категория базы знаний AgroBot (1–120 символов)")
    parser.add_argument("--demo", action="store_true",
                        help="демо: мок AgroBot + синтетическое аудио")
    parser.add_argument("--save-markdown", default=None,
                        help="куда сохранить Markdown до публикации")
    args = parser.parse_args()

    secretary = SecretaryClient(
        os.environ.get("SECRETARY_URL", "http://127.0.0.1:8080"),
        os.environ.get("SECRETARY_API_TOKEN", ""))
    agrobot_base = os.environ.get("AGROBOT_BASE_URL",
                                  DEMO_AGROBOT if args.demo else "")
    if args.demo and not os.environ.get("AGROBOT_INITDATA"):
        from agrobot_tma import make_demo_init_data
        initdata = make_demo_init_data("mock-bot-token")
    else:
        initdata = os.environ.get("AGROBOT_INITDATA", "")

    audio = Path(args.file or ("demo-sine.wav" if args.demo else ""))
    if args.demo and not audio.is_file():
        import subprocess
        print(f"демо: генерирую синтетическое аудио {audio}")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                        str(audio)], check=True)
    if not audio.is_file():
        parser.error(f"аудио-файл не найден: {audio}")

    print(f"[1/4] создаю задание расшифровки: {audio.name}")
    created = secretary.create_job(
        audio, filename=audio.name, language=args.language,
        vocabulary=[v.strip() for v in args.vocabulary.split(",") if v.strip()],
        owner=args.owner)
    print(f"      job_id={created['job_id']} "
          f"(дубликат: {not created['created']})")

    print("[2/4] жду завершения…")
    secretary.wait_terminal(created["job_id"])

    result = secretary.result(created["job_id"])
    markdown = secretary.markdown(created["job_id"])
    if args.save_markdown:
        Path(args.save_markdown).write_text(markdown, encoding="utf-8")
        print(f"      Markdown сохранён: {args.save_markdown}")

    print("[3/4] проверка человеком")
    if not human_confirm(result, markdown, yes=args.yes):
        print("публикация НЕ выполнена: подтверждение не получено")
        return 1

    # initData — кратковременная: возраст проверяется до публикации
    age = init_data_age_seconds(initdata)
    if initdata and age > INIT_DATA_MAX_AGE_SECONDS:
        print(f"initData просрочено ({age/3600:.1f} ч) — получите свежую "
              "сессию Mini App; это не постоянный сервисный токен")
        return 2
    if initdata and age > INIT_DATA_MAX_AGE_SECONDS / 2:
        print(f"ВНИМАНИЕ: initData возраст {age/3600:.1f} ч — скоро истечёт")

    print("[4/4] публикую в AgroBot как документ (после подтверждения)")
    if not agrobot_base:
        print("AGROBOT_BASE_URL не задан — публикация пропущена")
        return 1
    publish_name = f"transcript-{result['job_id'][:8]}.md"
    outcome = publish_to_agrobot(agrobot_base, initdata, markdown,
                                 filename=publish_name,
                                 category=args.category)
    print(f"      ответ AgroBot: {outcome['status']} {outcome['body'][:200]}")
    return 0 if outcome["status"] in (200, 201) else 2


if __name__ == "__main__":
    raise SystemExit(main())
