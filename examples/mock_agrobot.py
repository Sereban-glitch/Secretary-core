#!/usr/bin/env python3
"""Демонстрационный мок точки публикации AgroBot (только для примера).

Имитирует РЕАЛЬНЫЙ контракт админки «База знаний» (routes/admin-knowledge):
  POST /api/admin/knowledge/documents
    — multipart/form-data: files (один/несколько), category (обязательно,
      1–120), comment (опционально), force=1 (опционально);
    — Authorization: tma <URL-encoded initData> — проверяется НАСТОЯЩИМ
      образом: подпись HMAC-SHA256 по алгоритму Telegram (agrobot_tma.py),
      свежесть auth_date, allowlist админов. «Любая непустая строка»
      больше не принимается (см. ревью pass2, P1);
    — ответ как у реального сервера: 201 при accepted>0, иначе 200; тело
      {success, data:{results:[{ok,name,code,documentId,queued}],accepted}}
      — структурный ответ с per-file результатами приходит целиком.

Переменные окружения мока:
  MOCK_AGROBOT_BOT_TOKEN  — токен бота для проверки подписи (демо:
                            mock-bot-token);
  MOCK_AGROBOT_MAX_AGE    — срок жизни initData, с (по умолчанию 86400);
  MOCK_AGROBOT_ADMIN_IDS  — allowlist админов через запятую (демо: 700000001).

Реального кода, URL, ключей и initData AgroBot здесь нет. Файлы
складываются в ./mock-agrobot-uploads/. Запуск:
  python3 examples/mock_agrobot.py [порт]
"""
from __future__ import annotations

import json
import os
import sys
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agrobot_tma import InitDataError, verify_init_data  # noqa: E402

UPLOADS = Path(__file__).parent / "mock-agrobot-uploads"
BOT_TOKEN = os.environ.get("MOCK_AGROBOT_BOT_TOKEN", "mock-bot-token")
MAX_AGE = int(os.environ.get("MOCK_AGROBOT_MAX_AGE", str(24 * 60 * 60)))
ADMIN_IDS = {s.strip() for s in
             os.environ.get("MOCK_AGROBOT_ADMIN_IDS", "700000001").split(",")
             if s.strip()}


def verify_tma_header(header: str) -> dict:
    """Разбор Authorization как в tmaAuth: tma <URL-encoded initData>."""
    from urllib.parse import unquote
    if not header.startswith("tma "):
        raise InitDataError("missing")
    # сервер AgroBot делает decodeURIComponent(header.slice(4).trim())
    init_data = unquote(header[4:].strip())
    return verify_init_data(init_data, BOT_TOKEN, max_age_seconds=MAX_AGE,
                            admin_ids=ADMIN_IDS)


class MockAgrobotHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        # реальный маршрут AgroBot — /api/healthz (app.ts монтирует
        # routes/health.ts под /api); /healthz оставлен как legacy-алиас
        if self.path in ("/api/healthz", "/healthz"):
            body = json.dumps({"ok": True, "mock": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "not found")

    def do_POST(self):  # noqa: N802
        if self.path != "/api/admin/knowledge/documents":
            self.send_error(404, "not found")
            return
        # 1) авторизация: настоящий формат initData, не «любая строка»
        try:
            admin = verify_tma_header(self.headers.get("Authorization", ""))
        except InitDataError as exc:
            status = 403 if exc.reason == "not_admin" else 401
            self._json_error(status, f"initData rejected: {exc.reason}")
            return
        # 2) тело
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024 * 1024:
            self._json_error(400, "empty or oversized body")
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") +
            b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        # 3) поля формы как у реального сервера: category (обяз.),
        #    comment, force — это ЧАСТИ multipart без filename
        category = ""
        comment = ""
        force = False
        file_parts: list[tuple[str, bytes]] = []
        for part in message.iter_parts():
            if part.get_filename():
                file_parts.append((part.get_filename(),
                                   part.get_payload(decode=True) or b""))
                continue
            name = part.get_param("name", header="content-disposition")
            value = (part.get_payload(decode=True) or b"").decode(
                "utf-8", "replace")
            if name == "category":
                category = value.strip()
            elif name == "comment":
                comment = value.strip()
            elif name == "force":
                force = value.strip() in ("1", "true")
        if not (1 <= len(category) <= 120):
            self._json_error(400, "Выберите категорию")
            return
        # 4) файлы → per-file результат реальной формы
        results: list[dict] = []
        saved_paths: list[Path] = []
        for filename, payload in file_parts:
            safe = Path(filename).name
            target = UPLOADS / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            saved_paths.append(target)
            document_id = 1000 + len(saved_paths)
            results.append({
                "ok": True, "name": safe, "code": "created",
                "documentId": document_id, "queued": True,
            })
            print(f"[mock-agrobot] admin {admin['userId']}: документ {safe} "
                  f"({len(payload)} байт, категория {category!r}, "
                  f"force={force}, comment={comment or '—'}) — created, "
                  f"needs_review (проверка человеком обязательна)")
        if not results:
            self._json_error(400, "no files in multipart")
            return
        accepted = sum(1 for r in results if r["ok"])
        # реальная форма ответа: 201 при accepted>0, иначе 200
        response = json.dumps(
            {"success": accepted > 0,
             "data": {"results": results, "accepted": accepted}},
            ensure_ascii=False).encode("utf-8")
        self.send_response(201 if accepted > 0 else 200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"success": False,
                           "error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[mock-agrobot] {self.address_string()} {fmt % args}")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = ThreadingHTTPServer(("127.0.0.1", port), MockAgrobotHandler)
    print(f"[mock-agrobot] слушаю http://127.0.0.1:{port} "
          f"(загрузка: {UPLOADS})")
    print(f"[mock-agrobot] BOT_TOKEN={'*' * len(BOT_TOKEN)} "
          f"max_age={MAX_AGE}s admins={sorted(ADMIN_IDS)}")
    print("[mock-agrobot] POST /api/admin/knowledge/documents: multipart "
          "files+category, Authorization: tma <URL-encoded initData> — "
          "подпись проверяется по-настоящему")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-agrobot] остановлен")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
