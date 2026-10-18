"""Тесты проходов 3–4 оркестратора: область запуска, статусы этапов, итог.

Ревью pass3 (bea8b58):
- P1 «смешение импортов»: оркестратор обязан обрабатывать, показывать и
  публиковать только job_id текущего импорта. Регрессионный сценарий:
  в общей SQLite заранее лежит ЧУЖОЕ completed-задание A; импорт B
  публикует только B; второй архив C не публикует ни A, ни B;
- P1 «published: true при ошибке»: статусы считаются по фактическим
  результатам, частичные отказы видны в счётчиках, код возврата при
  failed — ненулевой;
- P2: healthz по реальному маршруту AgroBot /api/healthz; батчинг
  загрузок (не более 5 файлов и лимит байт на запрос); verify_admin
  (проверка по фиктивному токену) удалён.

Ревью pass4 (eb359c9):
- P1 «ложноположительный итог»: итог запуска учитывает ВСЕ аудио-задания
  текущего импорта, а не только HTTP-загрузки. Три независимых статуса
  (transcription/publication/workflow); пустой attempted=0 — не complete;
  четыре регрессионных сценария: только упавшее аудио; успешный документ
  + упавшее аудио; отсутствующее/оставшееся в очереди задание; архив,
  где все элементы отклонены.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).parent.parent
EXAMPLES = REPO / "examples"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(EXAMPLES))

from agrobot_tma import make_demo_init_data                     # noqa: E402
from mock_agrobot import MockAgrobotHandler, verify_tma_header  # noqa: E402

BOT = "mock-bot-token"


class RecordingAgrobotHandler(BaseHTTPRequestHandler):
    """Тестовый двойник AgroBot: реальный контракт + запись всех запросов.

    Правила имитации отказов (реальный сервер так не делает — это ручки
    для тестов частичных/полных отказов):
      имя файла с префиксом fail-  → per-file ok=False (как дубликат);
      имя файла с префиксом boom-  → весь запрос отвечает 500.
    """

    records: list = []  # [{"method", "path", "files": [имена]}]

    def do_GET(self):  # noqa: N802
        self._record()
        if self.path == "/api/healthz":
            self._json(200, {"ok": True, "mock": "recording"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        self._record()
        if self.path != "/api/admin/knowledge/documents":
            self._json(404, {"error": "not found"})
            return
        try:
            verify_tma_header(self.headers.get("Authorization", ""))
        except Exception as exc:  # noqa: BLE001 - InitDataError
            status = 403 if "not_admin" in str(exc) else 401
            self._json(status, {"success": False,
                                "error": f"initData rejected: {exc}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        names = self._multipart_files(body,
                                      self.headers.get("Content-Type", ""))
        self.records[-1]["files"] = names
        if any(n.startswith("boom-") for n in names):
            self._json(500, {"success": False, "error": "boom (test)"})
            return
        if not names:
            self._json(400, {"success": False, "error": "no files"})
            return
        results = []
        for i, name in enumerate(names):
            ok = not name.startswith("fail-")
            results.append({"ok": ok, "name": name,
                            "code": "created" if ok else "duplicate",
                            **({"documentId": 1000 + i, "queued": True}
                               if ok else {"error": "rejected by test"})})
        accepted = sum(1 for r in results if r["ok"])
        self._json(201 if accepted > 0 else 200,
                   {"success": accepted > 0,
                    "data": {"results": results, "accepted": accepted}})

    def _multipart_files(self, body: bytes, content_type: str) -> list[str]:
        from email import policy
        from email.parser import BytesParser
        if not body:
            return []
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1")
            + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        return [part.get_filename() for part in message.iter_parts()
                if part.get_filename()]

    def _record(self) -> None:
        self.records.append({"method": self.command, "path": self.path,
                             "files": []})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _spin_server(handler_class) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _ffmpeg_available() -> bool:
    return bool(subprocess.run(["sh", "-c", "command -v ffmpeg"],
                               capture_output=True).stdout.strip())


def _sine_mp3(path: Path, frequency: int = 440) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=3",
         str(path)], check=True, capture_output=True)


def _make_archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)


def _run_orchestrator(archive: Path, dest: Path, agrobot_url: str,
                      init_data: str, *, yes: bool = True,
                      extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp",
           "PYTHONIOENCODING": "utf-8",
           "SECRETARY_DB_PATH": str(dest / "db.sqlite"),
           "SECRETARY_JOBS_DIR": str(dest / "jobs"),
           "SECRETARY_MODEL": "mock-model"}
    env.update(extra_env or {})
    cmd = [sys.executable, str(EXAMPLES / "agrobot_orchestrator.py"),
           str(archive), "--category", "Регресс", "--fake-transport",
           "--dest", str(dest), "--agrobot-url", agrobot_url,
           "--init-data", init_data]
    if yes:
        cmd.append("--yes")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          cwd=str(REPO), env=env,
                          stdin=subprocess.DEVNULL)


class PublicationStatusUnitTests(unittest.TestCase):
    """compute_publication_status: статус этапа по факту (pass3 + pass4)."""

    def test_matrix(self):
        from agrobot_orchestrator import compute_publication_status as cps
        # pass4: пустой счётчик без ожидания — отдельный статус, НЕ complete
        self.assertEqual(cps({"ok": 0, "failed": 0}), "nothing_to_publish")
        self.assertEqual(cps({"ok": 2, "failed": 0}, expected=2), "complete")
        self.assertEqual(cps({"ok": 1, "failed": 1}, expected=2), "partial")
        self.assertEqual(cps({"ok": 0, "failed": 3}, expected=3), "failed")
        self.assertEqual(cps({"ok": 0, "failed": 0}, had_error=True),
                         "failed")  # публикация не началась

    def test_zero_attempts_with_expectation_is_not_complete(self):
        """pass4, требование 3: ожидалась публикация — пусто не «complete»."""
        from agrobot_orchestrator import compute_publication_status as cps
        self.assertEqual(cps({"ok": 0, "failed": 0}, expected=1), "failed")
        self.assertEqual(cps({"ok": 0, "failed": 0}, expected=2), "failed")
        # аномалия: попытки без ожидания не скрываются
        self.assertEqual(cps({"ok": 2, "failed": 0}), "complete")

    def test_exit_codes(self):
        from agrobot_orchestrator import EXIT_CODES
        self.assertEqual(EXIT_CODES,
                         {"complete": 0, "not_confirmed": 1,
                          "partial": 2, "failed": 3})
        for status, code in EXIT_CODES.items():
            if status == "complete":
                self.assertEqual(code, 0)
            else:
                self.assertNotEqual(code, 0)  # ненулевой иначе


class WorkflowStatusUnitTests(unittest.TestCase):
    """pass4: transcription/publication/workflow — чистые функции."""

    def test_transcription_status_matrix(self):
        from agrobot_orchestrator import compute_transcription_status as cts
        self.assertEqual(cts([], [])[0], "complete")  # аудио не ожидалось
        status, counts = cts(["a", "b"], ["completed", "completed"])
        self.assertEqual(status, "complete")
        self.assertEqual(counts["expected"], 2)
        self.assertEqual(counts["completed"], 2)
        # часть готова (включая частичное покрытие) — partial
        self.assertEqual(cts(["a", "b"], ["completed", "failed"])[0],
                         "partial")
        self.assertEqual(cts(["a", "b"], ["partial", "failed"])[0], "partial")
        self.assertEqual(cts(["a"], ["partial"])[0], "partial")
        # ни одного успешного результата — failed
        self.assertEqual(cts(["a"], ["failed"])[0], "failed")
        self.assertEqual(cts(["a", "b"], ["cancelled", "failed"])[0],
                         "failed")
        # отсутствующее в БД задание
        status, counts = cts(["a", "b"], [None, "completed"])
        self.assertEqual(status, "partial")
        self.assertEqual(counts["missing"], 1)
        self.assertEqual(cts(["a"], [None])[0], "failed")
        # оставшееся в очереди/выполняющееся
        status, counts = cts(["a"], ["queued"])
        self.assertEqual(status, "failed")
        self.assertEqual(counts["not_finished"], 1)
        self.assertEqual(cts(["a", "b"], ["completed", "running"])[0],
                         "partial")

    def test_workflow_status_matrix(self):
        from agrobot_orchestrator import compute_workflow_status as cws
        self.assertEqual(
            cws(transcription_status="complete",
                publication_status="complete",
                publication={"ok": 2, "failed": 0}), "complete")
        # ни одного опубликованного материала — всегда failed (pass4, P1)
        for t in ("failed", "partial", "complete"):
            for p in ("nothing_to_publish", "failed", "complete"):
                self.assertEqual(
                    cws(transcription_status=t, publication_status=p,
                        publication={"ok": 0, "failed": 0}), "failed",
                    f"{t}/{p}")
        # успешный документ не отменяет упавшую расшифровку
        self.assertEqual(
            cws(transcription_status="failed",
                publication_status="complete",
                publication={"ok": 1, "failed": 0}), "partial")
        self.assertEqual(
            cws(transcription_status="complete",
                publication_status="partial",
                publication={"ok": 1, "failed": 1}), "partial")
        # отсутствие подтверждения человеком — отдельный итог
        self.assertEqual(
            cws(transcription_status="complete",
                publication_status="not_confirmed",
                publication={"ok": 0, "failed": 0}), "not_confirmed")


class ClientBatchingTests(unittest.TestCase):
    """Батчинг загрузок: ≤5 файлов и лимит байт на запрос (P2)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _files(self, count: int, size: int = 10) -> list[Path]:
        paths = []
        for i in range(count):
            path = self.tmp / f"f{i:02d}.txt"
            path.write_bytes(b"x" * size)
            paths.append(path)
        return paths

    def test_batch_files_respects_max_files(self):
        from agrobot_client import _batch_files
        batches = _batch_files(self._files(12), max_files=5,
                               max_bytes=10**9)
        self.assertEqual([len(b) for b in batches], [5, 5, 2])
        self.assertEqual(sum(len(b) for b in batches), 12)

    def test_batch_files_respects_max_bytes(self):
        from agrobot_client import _batch_files
        batches = _batch_files(self._files(3, size=600), max_files=5,
                               max_bytes=1000)
        self.assertEqual([len(b) for b in batches], [1, 1, 1])

    def test_oversize_file_goes_alone(self):
        from agrobot_client import _batch_files
        big = self.tmp / "big.txt"
        big.write_bytes(b"x" * 2000)
        small = self.tmp / "small.txt"
        small.write_bytes(b"x" * 10)
        batches = _batch_files([small, big], max_files=5, max_bytes=1000)
        self.assertEqual([len(b) for b in batches], [1, 1])
        self.assertEqual(batches[1], [big])

    def test_batch_files_empty(self):
        from agrobot_client import _batch_files
        self.assertEqual(_batch_files([], max_files=5, max_bytes=1000), [])

    def test_default_limits_match_admin_contract(self):
        from agrobot_client import (AgrobotClient, DEFAULT_BATCH_MAX_BYTES,
                                    DEFAULT_BATCH_MAX_FILES)
        empty = {name: "" for name in (
            "AGROBOT_UPLOAD_BATCH_MAX_FILES",
            "AGROBOT_UPLOAD_BATCH_MAX_BYTES")}
        with mock.patch.dict(os.environ, empty, clear=False):
            client = AgrobotClient("http://127.0.0.1:1", "user=x")
        # 5 файлов — как MAX_FILES_PER_UPLOAD админки AgroBot
        self.assertEqual(client.batch_max_files, DEFAULT_BATCH_MAX_FILES)
        self.assertEqual(client.batch_max_files, 5)
        # меньше худшего случая 5×20 МиБ серверного лимита на файл
        self.assertLessEqual(DEFAULT_BATCH_MAX_BYTES, 5 * 20 * 1024 * 1024)

    def test_env_overrides_and_validation(self):
        from agrobot_client import AgrobotClient
        with mock.patch.dict(os.environ,
                             {"AGROBOT_UPLOAD_BATCH_MAX_FILES": "3",
                              "AGROBOT_UPLOAD_BATCH_MAX_BYTES": "1048576"}):
            client = AgrobotClient("http://127.0.0.1:1", "user=x")
        self.assertEqual(client.batch_max_files, 3)
        self.assertEqual(client.batch_max_bytes, 1048576)
        for bad in ("abc", "0", "-2"):
            with mock.patch.dict(
                    os.environ, {"AGROBOT_UPLOAD_BATCH_MAX_FILES": bad}):
                with self.assertRaises(ValueError):
                    AgrobotClient("http://127.0.0.1:1", "user=x")

    def test_verify_admin_removed(self):
        """P2: проверка по фиктивному server-token-unknown удалена."""
        from agrobot_client import AgrobotClient
        self.assertFalse(hasattr(AgrobotClient, "verify_admin"))


class ClientUploadBatchingE2ETests(unittest.TestCase):
    """Клиент против записывающего сервера: реальные HTTP-запросы."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = _spin_server(RecordingAgrobotHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        RecordingAgrobotHandler.records.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _client(self, **kwargs):
        from agrobot_client import AgrobotClient
        return AgrobotClient(self.url, make_demo_init_data(BOT), **kwargs)

    def test_healthz_uses_real_route(self):
        """P2: реальный маршрут AgroBot — /api/healthz (не /healthz)."""
        client = self._client()
        payload = client.healthz()
        self.assertTrue(payload.get("ok"))
        paths = [r["path"] for r in RecordingAgrobotHandler.records]
        self.assertIn("/api/healthz", paths)
        self.assertNotIn("/healthz", paths)

    def test_twelve_files_split_into_batches_of_five(self):
        files = [self.tmp / f"doc-{i:02d}.txt" for i in range(12)]
        for path in files:
            path.write_text("данные", encoding="utf-8")
        data = self._client().upload_documents(files, category="Батч")
        self.assertEqual(data["accepted"], 12)
        posts = [r for r in RecordingAgrobotHandler.records
                 if r["method"] == "POST"]
        self.assertEqual(len(posts), 3)  # ceil(12/5)
        for post in posts:
            self.assertLessEqual(len(post["files"]), 5)
        uploaded = [n for post in posts for n in post["files"]]
        self.assertEqual(sorted(uploaded),
                         sorted(p.name for p in files))

    def test_byte_cap_splits_earlier(self):
        files = [self.tmp / f"part-{i}.bin" for i in range(3)]
        for path in files:
            path.write_bytes(b"y" * 600)
        client = self._client(batch_max_files=5, batch_max_bytes=1000)
        data = client.upload_documents(files, category="Батч")
        self.assertEqual(data["accepted"], 3)
        posts = [r for r in RecordingAgrobotHandler.records
                 if r["method"] == "POST"]
        self.assertEqual([len(p["files"]) for p in posts], [1, 1, 1])

    def test_continue_on_error_collects_failures(self):
        """Отказавший батч не прерывает остальные: 5 failed + 2 ok."""
        files = [self.tmp / f"boom-{i}.txt" for i in range(5)]
        files += [self.tmp / f"ok-{i}.txt" for i in range(2)]
        for path in files:
            path.write_text("x", encoding="utf-8")
        client = self._client(batch_max_files=5)
        data = client.upload_documents(files, category="Батч",
                                       continue_on_error=True)
        oks = [r for r in data["results"] if r["ok"]]
        fails = [r for r in data["results"] if not r["ok"]]
        self.assertEqual(len(oks), 2)
        self.assertEqual(len(fails), 5)
        for item in fails:
            self.assertEqual(item["code"], "upload_failed")
            self.assertIn("500", item["error"])
        posts = [r for r in RecordingAgrobotHandler.records
                 if r["method"] == "POST"]
        self.assertEqual(len(posts), 2)  # второй батч всё равно отправлен

    def test_raise_mode_keeps_old_semantics(self):
        files = [self.tmp / "boom-1.txt", self.tmp / "ok-1.txt"]
        for path in files:
            path.write_text("x", encoding="utf-8")
        client = self._client()
        from agrobot_client import AgrobotClientError
        with self.assertRaises(AgrobotClientError):
            client.upload_documents(files, category="Батч")


class MockAgrobotHealthzTests(unittest.TestCase):
    """Мок обслуживает реальный маршрут /api/healthz (как AgroBot)."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = _spin_server(MockAgrobotHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_api_healthz_served(self):
        with urllib.request.urlopen(self.url + "/api/healthz",
                                     timeout=5) as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])


class OrchestratorRunScopeTests(unittest.TestCase):
    """РЕГРЕССИЯ P1 «смешение импортов» (требование ревью pass3).

    В общей SQLite заранее лежит completed-задание A (чужое). Импорт B
    публикует только B. Второй архив C не публикует ни A, ни B.
    """

    def setUp(self):
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dest = self.tmp / "run"
        self.dest.mkdir()
        RecordingAgrobotHandler.records.clear()
        self.server, self.url = _spin_server(RecordingAgrobotHandler)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _seed_foreign_completed_job(self) -> str:
        """Чужое завершённое задание A в общей БД оркестратора."""
        from secretary.config import SecretaryConfig
        from secretary.engine import TranscriptionEngine
        from secretary.store import Store
        from tests.fakes import (FakeProber, FakeTransport,
                                 make_fake_extractor, synthetic_audio)
        env = {"SECRETARY_MODEL": "mock-model",
               "SECRETARY_DB_PATH": str(self.dest / "db.sqlite"),
               "SECRETARY_JOBS_DIR": str(self.dest / "jobs")}
        config = SecretaryConfig.from_env(env)
        engine = TranscriptionEngine(
            config, FakeTransport(), Store(config.db_path),
            prober=FakeProber.probe, extractor=make_fake_extractor())
        audio = synthetic_audio(self.tmp / "old-meeting.ogg",
                                mime="audio/ogg", duration=61)
        created = engine.create_job(audio, filename="old-meeting.ogg",
                                    owner_id="someone-else")
        self.assertEqual(engine.run_job(created["job_id"]), "completed")
        engine.store.close()
        return created["job_id"]

    def _archive(self, name: str, *, with_audio: bool,
                 frequency: int) -> Path:
        members: dict[str, bytes] = {
            "brief.pdf": b"%PDF-1.4\nregression\n%%EOF\n"}
        if with_audio:
            mp3 = self.tmp / f"meeting-{frequency}.mp3"
            _sine_mp3(mp3, frequency)
            members["meeting.mp3"] = mp3.read_bytes()
        path = self.tmp / name
        _make_archive(path, members)
        return path

    def test_current_run_publishes_only_own_jobs(self):
        job_a = self._seed_foreign_completed_job()
        archive_b = self._archive("b.zip", with_audio=True, frequency=440)
        result = _run_orchestrator(archive_b, self.dest, self.url,
                                   make_demo_init_data(BOT))
        self.assertEqual(result.returncode, 0,
                         f"orchestrator B failed:\n{result.stdout}\n"
                         f"{result.stderr}")
        report_b = json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))
        self.assertEqual(report_b["publication_status"], "complete")
        job_ids_b = report_b["run"]["job_ids"]
        self.assertEqual(len(job_ids_b), 1)
        job_b = job_ids_b[0]
        self.assertNotEqual(job_b, job_a)        # чужое не примешано
        self.assertEqual(len(report_b["transcripts"]), 1)
        self.assertEqual(report_b["transcripts"][0]["name"],
                         f"transcript-{job_b[:8]}.md")
        # на сервер ушли ТОЛЬКО файлы запуска B
        posts_b = [r for r in RecordingAgrobotHandler.records
                   if r["method"] == "POST"]
        uploaded_b = {n for post in posts_b for n in post["files"]}
        self.assertIn("brief.pdf", uploaded_b)
        self.assertIn(f"transcript-{job_b[:8]}.md", uploaded_b)
        self.assertNotIn(f"transcript-{job_a[:8]}.md", uploaded_b)

        # ── второй архив C: не публикует ни A, ни B ──────────────────
        marker = len(RecordingAgrobotHandler.records)
        archive_c = self._archive("c.zip", with_audio=True, frequency=660)
        result_c = _run_orchestrator(archive_c, self.dest, self.url,
                                     make_demo_init_data(BOT))
        self.assertEqual(result_c.returncode, 0,
                         f"orchestrator C failed:\n{result_c.stdout}\n"
                         f"{result_c.stderr}")
        report_c = json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))
        job_ids_c = report_c["run"]["job_ids"]
        self.assertEqual(len(job_ids_c), 1)
        job_c = job_ids_c[0]
        self.assertNotIn(job_a, job_ids_c)
        self.assertNotIn(job_b, job_ids_c)
        # уникальный владелец у каждого запуска (нет «всего owner»)
        self.assertNotEqual(report_c["run"]["owner"],
                            report_b["run"]["owner"])
        # история отчётов по запускам сохранена
        history = sorted((self.dest / "reports").glob("report-*.json"))
        self.assertEqual(len(history), 2)
        # запросы запуска C не содержат ни A, ни B
        posts_c = [r for r in RecordingAgrobotHandler.records[marker:]
                   if r["method"] == "POST"]
        uploaded_c = {n for post in posts_c for n in post["files"]}
        self.assertEqual(uploaded_c,
                         {"brief.pdf", f"transcript-{job_c[:8]}.md"})
        self.assertNotIn(f"transcript-{job_a[:8]}.md", uploaded_c)
        self.assertNotIn(f"transcript-{job_b[:8]}.md", uploaded_c)


class OrchestratorPartialE2ETests(unittest.TestCase):
    """Частичный отказ: один документ отвергнут — partial, код 2."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = _spin_server(RecordingAgrobotHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        RecordingAgrobotHandler.records.clear()

    def test_partial_failure_visible_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run"
            dest.mkdir()
            archive = Path(tmp) / "docs.zip"
            _make_archive(archive, {
                "ok-note.txt": "хороший документ".encode("utf-8"),
                "fail-bad.txt": "документ-отказник".encode("utf-8")})
            result = _run_orchestrator(archive, dest, self.url,
                                       make_demo_init_data(BOT))
            self.assertEqual(result.returncode, 2,
                             f"ожидался exit 2 (partial):\n{result.stdout}")
            report = json.loads((dest / "report.json").read_text(
                encoding="utf-8"))
            self.assertEqual(report["publication_status"], "partial")
            self.assertTrue(report["published"])  # часть прошла
            self.assertEqual(report["publication"],
                             {"attempted": 2, "ok": 1, "failed": 1})
            failed = [d for d in report["documents"] if not d["ok"]]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["name"], "fail-bad.txt")
            self.assertEqual(failed[0]["code"], "duplicate")


class OrchestratorFailedE2ETests(unittest.TestCase):
    """Публикация не началась (initData просрочено) — failed, код 3."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = _spin_server(RecordingAgrobotHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        RecordingAgrobotHandler.records.clear()

    def test_stale_init_data_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run"
            dest.mkdir()
            archive = Path(tmp) / "docs.zip"
            _make_archive(archive, {"note.txt": "документ".encode("utf-8")})
            stale = make_demo_init_data(
                BOT, age_seconds=24 * 60 * 60 + 300)  # старше 24 ч
            result = _run_orchestrator(archive, dest, self.url, stale)
            self.assertEqual(result.returncode, 3,
                             f"ожидался exit 3 (failed):\n{result.stdout}")
            report = json.loads((dest / "report.json").read_text(
                encoding="utf-8"))
            self.assertEqual(report["publication_status"], "failed")
            self.assertFalse(report["published"])  # НЕ «true при ошибке»
            self.assertEqual(report["publication"],
                             {"attempted": 0, "ok": 0, "failed": 0})
            self.assertIn("просрочено", report["error"])
            # публикация не начиналась — запросов upload не было
            posts = [r for r in RecordingAgrobotHandler.records
                     if r["method"] == "POST"]
            self.assertEqual(posts, [])


class OrchestratorNotConfirmedTests(unittest.TestCase):
    """Без --yes и без терминала публикация не выполняется — код 1."""

    def test_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run"
            dest.mkdir()
            archive = Path(tmp) / "docs.zip"
            _make_archive(archive, {"note.txt": "документ".encode("utf-8")})
            result = _run_orchestrator(archive, dest, "http://127.0.0.1:1",
                                       "user=not-checked", yes=False)
            self.assertEqual(result.returncode, 1)
            report = json.loads((dest / "report.json").read_text(
                encoding="utf-8"))
            self.assertEqual(report["publication_status"], "not_confirmed")
            self.assertFalse(report["published"])
            self.assertEqual(report["documents"], [])


class Pass4RegressionE2ETests(unittest.TestCase):
    """РЕГРЕССИЯ P1 pass4 «ложноположительный итог» — 4 сценария ревью.

    1) только упавшее аудио            → workflow failed,  код 3;
    2) успешный документ + упавшее аудио → workflow partial, код 2;
    3) отсутствующий job_id (фазы) / задание, оставшееся в очереди
       (полный запуск: бюджет диска)    → workflow failed,  код 3;
    4) архив, где все элементы отклонены → workflow failed,  код 3.
    """

    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = _spin_server(RecordingAgrobotHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        RecordingAgrobotHandler.records.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dest = self.tmp / "run"
        self.dest.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _report(self) -> dict:
        return json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))

    def _posts(self) -> list[dict]:
        return [r for r in RecordingAgrobotHandler.records
                if r["method"] == "POST"]

    def _audio_archive(self, name: str, *, with_doc: bool) -> Path:
        members: dict[str, bytes] = {}
        if with_doc:
            members["brief.pdf"] = b"%PDF-1.4\nregression\n%%EOF\n"
        mp3 = self.tmp / "meeting.mp3"
        _sine_mp3(mp3)
        members["meeting.mp3"] = mp3.read_bytes()
        path = self.tmp / name
        _make_archive(path, members)
        return path

    # ── сценарий 1: только упавшее аудио ─────────────────────────────
    def test_failed_audio_only_workflow_failed(self):
        """Точное воспроизведение ревью: одна failed-расшифровка,
        документов нет; счётчики {0,0,0}; итог НЕ complete, код НЕ 0."""
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        archive = self._audio_archive("audio-only.zip", with_doc=False)
        result = _run_orchestrator(
            archive, self.dest, self.url, make_demo_init_data(BOT),
            extra_env={"SECRETARY_FAKE_TRANSPORT_FAIL": "1"})
        self.assertEqual(result.returncode, 3,
                         f"ожидался exit 3 (failed):\n{result.stdout}\n"
                         f"{result.stderr}")
        report = self._report()
        self.assertEqual(report["workflow_status"], "failed")
        self.assertEqual(report["transcription_status"], "failed")
        self.assertEqual(report["transcription"],
                         {"expected": 1, "completed": 0, "partial": 0,
                          "failed": 1, "cancelled": 0, "not_finished": 0,
                          "missing": 0})
        self.assertEqual(report["publication_status"], "nothing_to_publish")
        self.assertEqual(report["publication"],
                         {"attempted": 0, "ok": 0, "failed": 0})
        self.assertFalse(report["published"])
        self.assertTrue(report["reason"])  # пояснение к failed
        self.assertEqual(len(report["transcripts"]), 1)
        self.assertFalse(report["transcripts"][0]["published"])
        self.assertEqual(report["transcripts"][0]["reason"], "not completed")
        # в базу не попало НИЧЕГО — upload-запросов не было
        self.assertEqual(self._posts(), [])

    # ── сценарий 2: успешный документ + упавшее аудио ─────────────────
    def test_document_ok_audio_failed_workflow_partial(self):
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        archive = self._audio_archive("doc-and-audio.zip", with_doc=True)
        result = _run_orchestrator(
            archive, self.dest, self.url, make_demo_init_data(BOT),
            extra_env={"SECRETARY_FAKE_TRANSPORT_FAIL": "1"})
        self.assertEqual(result.returncode, 2,
                         f"ожидался exit 2 (partial):\n{result.stdout}")
        report = self._report()
        self.assertEqual(report["workflow_status"], "partial")
        # этапы НЕ смешиваются: расшифровка упала, публикация прошла
        self.assertEqual(report["transcription_status"], "failed")
        self.assertEqual(report["publication_status"], "complete")
        self.assertEqual(report["publication"],
                         {"attempted": 1, "ok": 1, "failed": 0})
        self.assertTrue(report["published"])  # документ в базе
        self.assertTrue(report["documents"][0]["ok"])
        self.assertFalse(report["transcripts"][0]["published"])
        # на сервер ушёл только документ
        uploaded = {n for post in self._posts() for n in post["files"]}
        self.assertEqual(uploaded, {"brief.pdf"})

    # ── сценарий 3b: задание осталось в очереди (полный запуск) ──────
    def test_job_left_queued_workflow_failed(self):
        """Нечестный бюджет диска (больше свободного места) — воркер
        возвращает задание в очередь и не доводит до терминального
        статуса; итог не может быть complete."""
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        archive = self._audio_archive("queued.zip", with_doc=False)
        result = _run_orchestrator(
            archive, self.dest, self.url, make_demo_init_data(BOT),
            extra_env={"SECRETARY_DISK_BUDGET_BYTES": "1099511627777"})
        self.assertEqual(result.returncode, 3,
                         f"ожидался exit 3 (failed):\n{result.stdout}")
        report = self._report()
        self.assertEqual(report["workflow_status"], "failed")
        self.assertEqual(report["transcription_status"], "failed")
        self.assertEqual(report["transcription"]["not_finished"], 1)
        self.assertEqual(report["transcription"]["completed"], 0)
        self.assertEqual(report["publication_status"], "nothing_to_publish")
        self.assertFalse(report["published"])
        self.assertEqual(self._posts(), [])

    # ── сценарий 3a: отсутствующий job_id (реальные фазы) ─────────────
    def test_missing_job_id_workflow_failed(self):
        """Строка задания исчезает из БД между импортом и воркером
        (сбой БД/ручное удаление) — единственный способ получить
        отсутствующий job_id из манифеста текущего импорта. Фазы
        оркестратора выполняются реальные, в порядке main()."""
        from agrobot_orchestrator import (EXIT_CODES, finalize_run,
                                          phase_import, phase_publish,
                                          phase_review, phase_transcribe)
        from secretary.config import SecretaryConfig
        from secretary.engine import TranscriptionEngine
        from secretary.store import Store
        from tests.fakes import (FakeProber, FakeTransport,
                                 make_fake_extractor, synthetic_audio)
        env = {"SECRETARY_MODEL": "mock-model",
               "SECRETARY_DB_PATH": str(self.dest / "db.sqlite"),
               "SECRETARY_JOBS_DIR": str(self.dest / "jobs")}
        config = SecretaryConfig.from_env(env)
        engine = TranscriptionEngine(
            config, FakeTransport(), Store(config.db_path),
            prober=FakeProber.probe, extractor=make_fake_extractor())
        synthetic_audio(self.tmp / "meeting.ogg", mime="audio/ogg",
                        duration=30)
        archive = self.tmp / "missing.zip"
        _make_archive(archive,
                      {"meeting.ogg": (self.tmp / "meeting.ogg").read_bytes()})
        args = Namespace(zip=str(archive), dest=str(self.dest),
                         run_id="runmissing01",
                         owner="agrobot-orchestrator-runmissing01",
                         language=None, category="Регресс", comment=None,
                         agrobot_url=self.url,
                         init_data=make_demo_init_data(BOT))
        imported = phase_import(args, engine)
        job_ids = [e["job_id"] for e in imported["manifest"]["entries"]
                   if e.get("kind") == "audio"]
        self.assertEqual(len(job_ids), 1)
        # сбой: строка задания потеряна между импортом и расшифровкой
        con = sqlite3.connect(config.db_path)
        con.execute("DELETE FROM jobs WHERE id = ?", (job_ids[0],))
        con.commit()
        con.close()
        transcribe = phase_transcribe(engine, job_ids)
        self.assertEqual(transcribe["status"], "failed")
        self.assertEqual(transcribe["counts"]["missing"], 1)
        self.assertEqual(transcribe["counts"]["expected"], 1)
        self.assertEqual(transcribe["transcripts"], [])
        self.assertTrue(phase_review(imported["documents"],
                                     transcribe["transcripts"], yes=True,
                                     run_id=args.run_id))
        upload_report = phase_publish(args, imported["documents"],
                                      transcribe["transcripts"])
        report, workflow = finalize_run(imported, transcribe, upload_report,
                                        category=args.category)
        self.assertEqual(workflow, "failed")
        self.assertEqual(EXIT_CODES[workflow], 3)
        self.assertEqual(report["transcription_status"], "failed")
        self.assertEqual(report["publication_status"], "nothing_to_publish")
        self.assertFalse(report["published"])
        self.assertEqual(self._posts(), [])
        engine.store.close()

    # ── сценарий 4: архив, где все элементы отклонены ─────────────────
    def test_all_rejected_archive_workflow_failed(self):
        archive = self.tmp / "garbage.zip"
        _make_archive(archive, {
            "tool.exe": b"MZ\x90\x00\x03\x00\x00\x00\x04\x00",
            "nested.zip": b"PK\x03\x04nested-archive"})
        result = _run_orchestrator(archive, self.dest, self.url,
                                   make_demo_init_data(BOT))
        self.assertEqual(result.returncode, 3,
                         f"ожидался exit 3 (failed):\n{result.stdout}")
        report = self._report()
        self.assertEqual(report["workflow_status"], "failed")
        # аудио не ожидалось — этап расшифровки тривиально complete
        self.assertEqual(report["transcription_status"], "complete")
        self.assertEqual(report["transcription"]["expected"], 0)
        self.assertEqual(report["publication_status"], "nothing_to_publish")
        self.assertFalse(report["published"])
        self.assertEqual(report["import"]["rejected"], 2)
        self.assertEqual(report["import"]["documents"], 0)
        self.assertEqual(report["import"]["audio_jobs"], 0)
        self.assertTrue(report["reason"])
        self.assertEqual(self._posts(), [])


if __name__ == "__main__":
    unittest.main()
