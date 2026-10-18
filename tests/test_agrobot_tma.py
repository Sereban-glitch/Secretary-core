"""Тесты контракта Telegram initData и мока AgroBot (ревью pass2, P1).

Проверяется РЕАЛЬНЫЙ формат авторизации (как в AgroBot tmaAuth):
- подпись HMAC по официальному алгоритму Telegram;
- свежесть auth_date (устаревшее/будущее — отказ);
- allowlist админов (403), а не «любая непустая строка»;
- заголовок tma <URL-encoded initData> — сырая строка с percent-escaping
  не проходит (как в реальном сервере: decodeURIComponent ломает подпись);
- форма ответа мока = реальный контракт {success, data:{results, accepted}}.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

EXAMPLES = Path(__file__).parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES))

from agrobot_tma import (InitDataError, encode_init_data_header,  # noqa: E402
                         init_data_age_seconds, make_demo_init_data,
                         sign_init_data, verify_init_data)
from mock_agrobot import (ADMIN_IDS, BOT_TOKEN, MAX_AGE,  # noqa: E402
                          MockAgrobotHandler)

# мок читает токен/лимиты из env при импорте — тесты подписывают тем же
BOT = BOT_TOKEN
ADMIN = int(sorted(ADMIN_IDS)[0]) if ADMIN_IDS else 700000001


def signed(user_id: int = ADMIN, age: float = 5.0,
           auth_date: int | None = None, bot_token: str | None = None,
           tricky_name: bool = True) -> str:
    """initData с «трудными» символами в значениях (&, =) — как в реальных
    данных (имена/юзернеймы содержат спец-символы); именно на них сырая
    (не URL-encoded) передача ломается с 401 на настоящем сервере.
    """
    import time as _time
    from urllib.parse import urlencode
    import hashlib
    import hmac as _hmac
    token = bot_token or BOT
    name = "Тест & Ко=1" if tricky_name else "Тест"
    user = json.dumps({"id": user_id, "first_name": name,
                       "username": "t_admin"}, ensure_ascii=False)
    fields = {"auth_date": str(auth_date if auth_date is not None
                               else int(_time.time() - age)),
              "query_id": "AAEtest-query",
              "user": user}
    pairs = list(fields.items())
    secret = _hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs))
    digest = _hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs + [("hash", digest)])


class TmaSignVerifyTests(unittest.TestCase):
    def test_roundtrip(self):
        data = signed()
        verified = verify_init_data(data, BOT, admin_ids={str(ADMIN)})
        self.assertEqual(verified["userId"], ADMIN)

    def test_make_demo_init_data_roundtrip(self):
        data = make_demo_init_data(BOT)
        verified = verify_init_data(data, BOT, admin_ids={"700000001"})
        self.assertEqual(verified["userId"], 700000001)

    def test_wrong_token_rejected(self):
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(signed(), "other-token")
        self.assertEqual(ctx.exception.reason, "bad_signature")

    def test_tampered_payload_rejected(self):
        data = signed()
        tampered = data.replace("query_id=AAEtest", "query_id=AAEtampe")
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(tampered, BOT)
        self.assertEqual(ctx.exception.reason, "bad_signature")

    def test_missing_hash_rejected(self):
        from urllib.parse import parse_qsl, urlencode
        pairs = [(k, v) for k, v in parse_qsl(signed()) if k != "hash"]
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(urlencode(pairs), BOT)
        self.assertEqual(ctx.exception.reason, "missing_hash")

    def test_stale_rejected(self):
        data = signed(auth_date=int(time.time() - MAX_AGE - 10))
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(data, BOT, max_age_seconds=MAX_AGE)
        self.assertEqual(ctx.exception.reason, "stale")

    def test_future_rejected(self):
        data = signed(auth_date=int(time.time() + 3600))
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(data, BOT)
        self.assertEqual(ctx.exception.reason, "future")

    def test_not_admin_rejected(self):
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(signed(user_id=ADMIN + 1), BOT,
                             admin_ids={str(ADMIN)})
        self.assertEqual(ctx.exception.reason, "not_admin")

    def test_bad_user_json_rejected(self):
        from urllib.parse import parse_qsl, urlencode
        import hashlib
        import hmac as _hmac
        pairs = [("auth_date", str(int(time.time()))),
                 ("user", "{not json")]
        secret = _hmac.new(b"WebAppData", BOT.encode(), hashlib.sha256).digest()
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs))
        digest = _hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        data = urlencode(pairs + [("hash", digest)])
        with self.assertRaises(InitDataError) as ctx:
            verify_init_data(data, BOT)
        self.assertEqual(ctx.exception.reason, "bad_user")

    def test_header_encoding_matches_js_encodeuricomponent(self):
        """quote(safe="!~*'()") == encodeURIComponent по алфавиту."""
        data = signed()
        header = encode_init_data_header(data)
        self.assertTrue(header.startswith("tma "))
        encoded = header[4:]
        # JS encodeURIComponent не трогает A-Za-z0-9-_.!~*'() и кодирует &=%
        self.assertNotIn("&", encoded)
        self.assertNotIn("=", encoded)
        self.assertNotIn("%22", encoded.replace("%2522", ""))  # двойное кодирование
        # декодирование даёт исходную строку
        from urllib.parse import unquote
        self.assertEqual(unquote(encoded), data)

    def test_age_helper(self):
        self.assertAlmostEqual(init_data_age_seconds(signed(age=30)), 30,
                               delta=2)
        self.assertEqual(init_data_age_seconds("user=x"), float("inf"))


class MockAgrobotContractTests(unittest.TestCase):
    """Мок проверяет настоящий формат: E2E по HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         MockAgrobotHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                       daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _upload(self, init_data: str, *, raw_header: bool = False,
                category: str = "Хорш", files=None) -> tuple[int, dict]:
        boundary = "----test-boundary"
        parts = []
        for name, content in (files or [("note.md",
                                         "# Расшифровка синтетическая"
                                         .encode("utf-8"))]):
            parts.append(
                (f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"files\"; "
                 f"filename=\"{name}\"\r\n"
                 f"Content-Type: text/markdown\r\n\r\n").encode()
                + content + b"\r\n")
        parts.append((f"--{boundary}\r\n"
                      f"Content-Disposition: form-data; "
                      f"name=\"category\"\r\n\r\n{category}\r\n").encode())
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        request = urllib.request.Request(
            self._url() + "/api/admin/knowledge/documents",
            data=body, method="POST")
        request.add_header("Content-Type",
                           f"multipart/form-data; boundary={boundary}")
        # как настоящий клиент: URL-encoded initData (или сырой — для негативного теста)
        request.add_header("Authorization",
                           f"tma {init_data}" if raw_header
                           else encode_init_data_header(init_data))
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_signed_upload_real_contract_response(self):
        status, payload = self._upload(signed())
        self.assertEqual(status, 201)
        # РЕАЛЬНАЯ форма ответа, не выдуманный {status: needs_review}
        self.assertTrue(payload["success"])
        data = payload["data"]
        self.assertEqual(data["accepted"], 1)
        self.assertEqual(len(data["results"]), 1)
        item = data["results"][0]
        self.assertTrue(item["ok"])
        self.assertEqual(item["name"], "note.md")
        self.assertIn("documentId", item)
        self.assertIn("queued", item)

    def test_unsigned_header_rejected(self):
        """«Любая непустая строка» больше не проходит (дефект P1)."""
        status, payload = self._upload("any-non-empty-string")
        self.assertEqual(status, 401)
        self.assertIn("initData rejected", payload["error"])

    def test_wrong_bot_token_rejected(self):
        """Подписано токеном ДРУГОЙ инсталляции бота — подпись не сходится."""
        other = signed(bot_token="completely-different-bot-token")
        status, payload = self._upload(other)
        self.assertEqual(status, 401)
        self.assertIn("bad_signature", payload["error"])

    def test_raw_unencoded_header_rejected(self):
        """Сырая (не URL-encoded) initData: значения с &/= ломают разбор — 401.

        Ровно риск P1-дефекта: старый адаптер слал сырую строку; на значениях
        без спец-символов это могло случайно работать, на реальных данных
        (имена с & и т.п.) — 401 на настоящем сервере AgroBot.
        """
        status, payload = self._upload(signed(), raw_header=True)
        self.assertEqual(status, 401)
        self.assertIn("initData rejected", payload["error"])

    def test_stale_init_data_rejected(self):
        old = signed(auth_date=int(time.time() - MAX_AGE - 60))
        status, payload = self._upload(old)
        self.assertEqual(status, 401)
        self.assertIn("stale", payload["error"])

    def test_not_admin_rejected_403(self):
        status, payload = self._upload(signed(user_id=800000002))
        self.assertEqual(status, 403)
        self.assertIn("not_admin", payload["error"])

    def test_missing_category_rejected(self):
        status, payload = self._upload(signed(), category="")
        self.assertEqual(status, 400)
        self.assertIn("категор", payload["error"])

    def test_category_too_long_rejected(self):
        status, payload = self._upload(signed(), category="к" * 121)
        self.assertEqual(status, 400)


class OrchestratorScriptedTests(unittest.TestCase):
    """Смоук сценария интеграции: ZIP(PDF+MP3) → мок AgroBot (реальный контракт)."""

    def test_orchestrator_smoke(self):
        ffmpeg = subprocess.run(["sh", "-c", "command -v ffmpeg"],
                                capture_output=True).stdout.strip()
        if not ffmpeg:
            self.skipTest("ffmpeg недоступен")
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "stage"
            staging.mkdir()
            (staging / "brief.pdf").write_bytes(b"%PDF-1.4\nsmoke\n%%EOF\n")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 str(staging / "meeting-01.mp3")], check=True,
                capture_output=True)
            archive = Path(tmp) / "archive.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for path in sorted(staging.iterdir()):
                    zf.write(path, path.name)

            mock = subprocess.Popen(
                [sys.executable, str(EXAMPLES / "mock_agrobot.py"), "8097"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.8)
                result = subprocess.run(
                    [sys.executable,
                     str(EXAMPLES / "agrobot_orchestrator.py"), str(archive),
                     "--category", "Смоук", "--fake-transport", "--yes",
                     "--demo", "--dest", str(Path(tmp) / "run"),
                     "--agrobot-url", "http://127.0.0.1:8097"],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(EXAMPLES.parent),
                    env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                         "MOCK_AGROBOT_BOT_TOKEN": "mock-bot-token",
                         "HOME": "/tmp"})
                self.assertEqual(result.returncode, 0,
                                 f"orchestrator failed:\n{result.stdout}\n"
                                 f"{result.stderr}")
                report = json.loads(
                    (Path(tmp) / "run" / "report.json").read_text(
                        encoding="utf-8"))
                self.assertTrue(report["published"])
                self.assertEqual(report["publication_status"], "complete")
                self.assertEqual(report["publication"]["failed"], 0)
                self.assertEqual(report["import"]["documents"], 1)
                self.assertEqual(report["import"]["audio_jobs"], 1)
                self.assertEqual(len(report["documents"]), 1)
                self.assertTrue(report["documents"][0]["ok"])
                self.assertEqual(len(report["transcripts"]), 1)
                self.assertTrue(report["transcripts"][0]["published"])
            finally:
                mock.terminate()
                mock.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
