"""Тесты прохода 5 (+корректирующий проход по ревью 49a79b1):
утилита проверки одной живой записи (proxy_check).

Сценарии из ТЗ прохода 5:
1.  doctor в полностью готовой среде;
2.  нет ffmpeg;
3.  недостаточно места;
4.  некорректный URL прокси;
5.  dry-run не делает сетевых запросов;
6.  live без двойного подтверждения блокируется;
7.  успешный replay (возобновление незавершённого задания);
8.  временная и постоянная ошибки replay;
9.  битый JSON и пустой ответ — честная неудача;
10. отчёт не содержит ключ, полный URL и полный текст;
11. пиковая память честно «unavailable» при недоступном /proc;
12. ZIP с несколькими аудио не выбирается молча без параметра;
13. review-package сохраняет папки и manifest (+ права 0700/0600);
14. временный аудиофрагмент удаляется после успеха;
15. предыдущие проходы не сломаны — полный прогон suite.

Сценарии корректирующего прохода (ревью 49a79b1):
16. ни одного символа расшифровки в отчёте по умолчанию (JSON и MD);
17. текст в отчёте — только явным флагом --include-transcript,
    и тогда отчёт закрыт правами 0600;
18. live обрабатывает ТОЛЬКО локально вырезанный короткий фрагмент
    (задание из фрагмента, не из всей записи; исходник не меняется;
    временный фрагмент удалён; в транспорт уходит только фрагмент);
19. длительность записи в отчёте — не null;
20. контрольные суммы (sha256), размеры и раздельное время этапов;
21. пакет материалов полон: doctor.json + собственная опись
    package-manifest.json с суммами каждого файла.

Живой успех через приватный прокси в среде разработки НЕ проверяется
и не имитируется: доступа к прокси нет (docs/PROXY.md). Live-режим
тестируется на блокировку без двойного подтверждения и на вырезание
фрагмента с тестовым двойником транспорта (сети нет).
"""
import base64
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).parent.parent
EXAMPLES = REPO / "examples"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(EXAMPLES))

import proxy_check                                     # noqa: E402
from secretary.config import SecretaryConfig           # noqa: E402
from secretary.engine import TranscriptionEngine       # noqa: E402
from secretary.errors import TransientTransportError   # noqa: E402
from secretary.store import Store                      # noqa: E402
from secretary.transport import HttpTransport          # noqa: E402
from tests.fakes import (FakeProber, FakeTransport,         # noqa: E402
                         ScriptedTransport,
                         make_fake_extractor, ok_text,
                         synthetic_audio)


def _ffmpeg_available() -> bool:
    return bool(subprocess.run(["sh", "-c", "command -v ffmpeg"],
                               capture_output=True).stdout.strip())


def _sine_mp3(path: Path, duration: int = 2) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-codec:a", "libmp3lame", "-b:a", "32k", str(path)],
        check=True, capture_output=True)


def _make_zip(path: Path, members: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _run_tool(args_list, *, extra_env: dict | None = None,
              timeout: int = 240) -> subprocess.CompletedProcess:
    """CLI-прогон утилиты в чистом окружении (без случайных SECRETARY_*)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": "/tmp", "PYTHONIOENCODING": "utf-8"}
    env.update(extra_env or {})
    cmd = [sys.executable, str(EXAMPLES / "proxy_check.py")] + args_list
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=str(REPO), env=env,
                          stdin=subprocess.DEVNULL)


_TINY_RETRY = {"SECRETARY_BACKOFF_BASE_SECONDS": "0.01",
               "SECRETARY_BACKOFF_MAX_SECONDS": "0.02",
               "SECRETARY_MAX_ATTEMPTS": "3"}


class DoctorTests(unittest.TestCase):
    """doctor: готовность среды (сценарии 1–4)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _main(self, env_extra: dict):
        env = {"SECRETARY_JOBS_DIR": str(self.tmp / "jobs")}
        env.update(env_extra)
        with mock.patch.dict(os.environ, env, clear=False):
            code = proxy_check.main(
                ["doctor", "--dest", str(self.tmp / "dest")])
        return code, (self.tmp / "dest" / "doctor.json")

    def test_doctor_ready(self):
        """Сценарий 1: всё задано (https-прокси, модель, ключ, ffmpeg)."""
        with mock.patch("shutil.which",
                        side_effect=lambda n: f"/usr/bin/{n}"):
            code, doctor = self._main({
                "SECRETARY_PROXY_BASE_URL": "https://proxy.example.test",
                "SECRETARY_MODEL": "test-model",
                "SECRETARY_PROXY_API_KEY": "k",
            })
        self.assertEqual(code, 0)
        data = json.loads(doctor.read_text(encoding="utf-8"))
        self.assertTrue(data["dry_run_ready"])
        self.assertTrue(data["live_ready"])

    def test_doctor_ready_for_dry_run_without_proxy_settings(self):
        """Без настроек прокси dry-run ГОТОВ (модель/ключ/URL — только
        для live, это предупреждения, не блокеры dry-run)."""
        with mock.patch("shutil.which",
                        side_effect=lambda n: f"/usr/bin/{n}"):
            code, doctor = self._main({})
        self.assertEqual(code, 0)
        data = json.loads(doctor.read_text(encoding="utf-8"))
        self.assertTrue(data["dry_run_ready"])
        self.assertFalse(data["live_ready"])

    def test_doctor_no_ffmpeg(self):
        """Сценарий 2: ffmpeg/ffprobe не найдены — dry-run НЕ ГОТОВ."""
        with mock.patch("shutil.which", return_value=None):
            code, doctor = self._main({
                "SECRETARY_PROXY_BASE_URL": "https://proxy.example.test",
                "SECRETARY_MODEL": "test-model",
                "SECRETARY_PROXY_API_KEY": "k",
            })
        self.assertEqual(code, 3)
        data = json.loads(doctor.read_text(encoding="utf-8"))
        self.assertFalse(data["dry_run_ready"])
        names = {c["name"] for c in data["checks"] if not c["ok"]}
        self.assertIn("ffmpeg", names)
        self.assertIn("ffprobe", names)

    def test_doctor_insufficient_disk(self):
        """Сценарий 3: бюджет места больше свободного — FAIL."""
        free = __import__("shutil").disk_usage(self.tmp).free
        code, doctor = self._main({
            "SECRETARY_DISK_BUDGET_BYTES": str(free + 1_000_000_000),
        })
        self.assertEqual(code, 3)
        data = json.loads(doctor.read_text(encoding="utf-8"))
        names = {c["name"] for c in data["checks"] if not c["ok"]}
        self.assertIn("место под задания", names)

    def test_doctor_bad_proxy_url(self):
        """Сценарий 4: http вне loopback — конфигурация отвергнута."""
        code, doctor = self._main({
            "SECRETARY_PROXY_BASE_URL": "http://example.com",
        })
        self.assertEqual(code, 3)
        data = json.loads(doctor.read_text(encoding="utf-8"))
        self.assertFalse(data["dry_run_ready"])
        self.assertIn("config_error", data)

    def test_doctor_bad_proxy_url_with_v1_suffix(self):
        code, _ = self._main({
            "SECRETARY_PROXY_BASE_URL": "https://proxy.example.test/v1",
        })
        self.assertEqual(code, 3)


class LiveGateTests(unittest.TestCase):
    """Сценарий 6: live без двойного подтверждения блокируется."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"

    def _run(self, argv_extra, env_extra):
        env = {"SECRETARY_JOBS_DIR": str(self.tmp / "jobs")}
        env.update(env_extra)
        argv = ["run", str(self.tmp / "note.mp3"),
                "--dest", str(self.dest)] + argv_extra
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(socket, "socket",
                                  side_effect=AssertionError(
                                      "сеть в live-блокировке")):
            return proxy_check.main(argv)

    def test_live_without_any_confirmation_blocked(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--live"], {})
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse((self.dest / "db.sqlite").exists())
        self.assertFalse((self.dest / "jobs").exists())

    def test_live_env_only_still_blocked(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--live"], {"SECRETARY_LIVE_CONFIRM": "1"})
        self.assertEqual(ctx.exception.code, 2)

    def test_live_flag_only_still_blocked(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--live", "--live-yes"], {})
        self.assertEqual(ctx.exception.code, 2)

    def test_live_confirmed_but_no_settings_blocked(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--live", "--live-yes"],
                      {"SECRETARY_LIVE_CONFIRM": "1"})
        self.assertEqual(ctx.exception.code, 2)

    def test_live_confirmed_bad_url_config_rejected(self):
        code = self._run(["--live", "--live-yes"],
                         {"SECRETARY_LIVE_CONFIRM": "1",
                          "SECRETARY_PROXY_BASE_URL": "http://example.com",
                          "SECRETARY_MODEL": "m"})
        self.assertEqual(code, 3)  # ConfigError поймана, трейсбека нет


class SelectionTests(unittest.TestCase):
    """Сценарий 12: несколько аудио в архиве — явный выбор обязателен."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        mp3 = self.tmp / "a.mp3"
        _sine_mp3(mp3)
        self.multi = self.tmp / "multi.zip"
        _make_zip(self.multi, {"one.mp3": mp3.read_bytes(),
                               "two.mp3": mp3.read_bytes(),
                               "brief.pdf": b"%PDF-1.4\n%%EOF\n"})
        self.single = self.tmp / "single.zip"
        _make_zip(self.single, {"only.mp3": mp3.read_bytes()})
        self.empty = self.tmp / "empty.zip"
        _make_zip(self.empty, {"brief.pdf": b"%PDF-1.4\n%%EOF\n"})

    def _run(self, argv_extra, extra_env=None):
        return _run_tool(["run", str(self.multi), "--dest", str(self.dest)]
                         + argv_extra, extra_env=extra_env)

    def test_multi_audio_requires_selection(self):
        result = self._run([])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("one.mp3", result.stdout)
        self.assertIn("--select", result.stdout)
        # заданий не создано — выбора не было
        self.assertFalse((self.dest / "db.sqlite").exists())

    def test_index_out_of_range_rejected(self):
        result = self._run(["--index", "9"])
        self.assertEqual(result.returncode, 1)

    def test_select_unknown_name_rejected(self):
        result = self._run(["--select", "nope.mp3"])
        self.assertEqual(result.returncode, 1)

    def test_select_by_name_processes_one(self):
        result = self._run(["--select", "two.mp3"])
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))
        self.assertEqual(report["transcription"]["expected"], 1)
        self.assertEqual(report["selection"]["selected"], ["two.mp3"])

    def test_select_by_index_processes_one(self):
        result = self._run(["--index", "1"])
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))
        self.assertEqual(report["selection"]["selected"], ["one.mp3"])

    def test_single_audio_zip_selected_automatically(self):
        result = _run_tool(["run", str(self.single),
                            "--dest", str(self.dest)])
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))
        self.assertEqual(report["selection"]["selected"], ["only.mp3"])

    def test_zip_without_audio_rejected(self):
        result = _run_tool(["run", str(self.empty),
                            "--dest", str(self.dest)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("нет аудиозаписей", result.stdout)


class VocabularyTests(unittest.TestCase):
    def test_combines_deduplicates_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "terms.txt"
            path.write_text("# note\nHORSCH\n обприскувач \n",
                            encoding="utf-8")
            result = proxy_check.parse_vocabulary(
                "LEEB, horsch, сервіс", str(path))
        self.assertEqual(result, ["LEEB", "horsch", "сервіс",
                                  "обприскувач"])

    def test_missing_file_rejected(self):
        with self.assertRaises(ValueError):
            proxy_check.parse_vocabulary(None, "/missing/terms.txt")

    def test_do_import_forwards_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "note.mp3"
            audio.write_bytes(b"fake")
            engine = mock.Mock()
            engine.create_job.return_value = {"job_id": "job-1"}
            manifest = proxy_check.do_import(
                engine,
                {"type": "file", "path": str(audio),
                 "selected": ["note.mp3"]},
                owner="owner", language="uk",
                vocabulary=["HORSCH", "обприскувач"], dest=Path(tmp))
        self.assertEqual(manifest["entries"][0]["job_id"], "job-1")
        engine.create_job.assert_called_once_with(
            audio, owner_id="owner", language="uk",
            vocabulary=["HORSCH", "обприскувач"], filename="note.mp3")


class DryRunTests(unittest.TestCase):
    """Сценарии 5 и базовые прогоны (dry-run, без сети)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self.mp3 = self.tmp / "note.mp3"
        _sine_mp3(self.mp3)

    def _report(self) -> dict:
        return json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))

    def test_dry_run_makes_no_network_requests(self):
        """Сценарий 5: сокет заблокирован — dry-run проходит полностью."""
        env = {"SECRETARY_DB_PATH": str(self.dest / "db.sqlite"),
               "SECRETARY_JOBS_DIR": str(self.dest / "jobs")}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(socket, "socket",
                                  side_effect=AssertionError(
                                      "dry-run не должен открывать сокеты")):
            code = proxy_check.main(
                ["run", str(self.mp3), "--dest", str(self.dest)])
        self.assertEqual(code, 0)
        report = self._report()
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["workflow_status"], "complete")

    def test_dry_run_success_report_and_package(self):
        result = _run_tool(["run", str(self.mp3), "--dest", str(self.dest)])
        self.assertEqual(result.returncode, 0, result.stdout)
        report = self._report()
        self.assertEqual(report["workflow_status"], "complete")
        self.assertEqual(report["transcription"]["completed"], 1)
        job = report["jobs"][0]
        self.assertEqual(job["status"], "completed")
        self.assertGreater(job["transcript_chars"], 0)
        # ни одного символа расшифровки по умолчанию — ни ключа, ни текста
        self.assertNotIn("preview", job)
        self.assertNotIn("transcript", job)
        self.assertFalse(report["transcript_included"])
        # длительность записи — не null (сценарий 19)
        self.assertIsNotNone(job["duration_seconds"])
        self.assertAlmostEqual(job["duration_seconds"], 2.0, delta=0.8)
        # контрольные суммы и размеры (сценарий 20)
        self.assertEqual(job["source_bytes"], self.mp3.stat().st_size)
        self.assertEqual(job["source_sha256"],
                         proxy_check.sha256_of(self.mp3))
        self.assertEqual(report["source"]["sha256"],
                         proxy_check.sha256_of(self.mp3))
        self.assertEqual(report["source"]["size_bytes"],
                         self.mp3.stat().st_size)
        # раздельное время этапов (сценарий 20)
        for key in ("select_seconds", "fragment_seconds", "import_seconds",
                    "transcribe_seconds", "network_seconds",
                    "report_seconds", "total_seconds"):
            self.assertIn(key, report["timings"], key)
        self.assertGreaterEqual(report["timings"]["total_seconds"],
                                report["timings"]["transcribe_seconds"])
        self.assertEqual(report["agrobot"]["publication"], "none")
        self.assertEqual(report["agrobot"]["requests"], 0)
        self.assertGreaterEqual(report["transport"]["requests"], 2)
        self.assertTrue(report["memory"]["available"])
        self.assertGreater(report["memory"]["peak_rss_kb"], 0)
        self.assertIn("total_seconds", report["timings"])
        # пакет материалов
        review = self.dest / f"review-{report['run_id']}"
        self.assertTrue((review / "report.json").is_file())
        self.assertTrue((review / "report.md").is_file())
        self.assertTrue((review / "check-run.json").is_file())
        transcripts = list((review / "jobs").glob("transcript-*.md"))
        self.assertEqual(len(transcripts), 1)

    def test_failed_transport_honest_failure(self):
        result = _run_tool(["run", str(self.mp3), "--dest", str(self.dest)],
                           extra_env={"SECRETARY_FAKE_TRANSPORT_FAIL": "1"})
        self.assertEqual(result.returncode, 3, result.stdout)
        report = self._report()
        self.assertEqual(report["workflow_status"], "failed")
        self.assertEqual(report["transcription_status"], "failed")
        self.assertEqual(report["jobs"][0]["status"], "failed")
        self.assertIn("reason", report)
        # у упавшего задания полного текста в пакете нет
        review = self.dest / f"review-{report['run_id']}"
        self.assertEqual(list((review / "jobs").glob("transcript-*.md")), [])

    def test_transient_errors_retried_within_run(self):
        """Bounded retry: 2 временные ошибки затем успех — итог complete."""
        result = _run_tool(["run", str(self.mp3), "--dest", str(self.dest)],
                           extra_env={"SECRETARY_FAKE_TRANSPORT_TRANSIENT":
                                      "2", **_TINY_RETRY})
        self.assertEqual(result.returncode, 0, result.stdout)
        report = self._report()
        self.assertEqual(report["workflow_status"], "complete")
        self.assertGreater(report["transport"]["requests"], 2)


class ReportAnonymizationTests(unittest.TestCase):
    """Сценарий 10: в отчёте нет ключа, полного URL и полного текста."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self.mp3 = self.tmp / "note.mp3"
        _sine_mp3(self.mp3)

    def test_report_has_no_secrets_and_no_full_text(self):
        secret = "klyuch-sekret-123"
        base = "https://proxy.example.invalid"
        result = _run_tool(
            ["run", str(self.mp3), "--dest", str(self.dest)],
            extra_env={"SECRETARY_PROXY_API_KEY": secret,
                       "SECRETARY_PROXY_BASE_URL": base,
                       "SECRETARY_MODEL": "mock-model"})
        self.assertEqual(result.returncode, 0, result.stdout)
        report_raw = (self.dest / "report.json").read_text(encoding="utf-8")
        markdown = (self.dest / "report.md").read_text(encoding="utf-8")
        report = json.loads(report_raw)
        self.assertNotIn(secret, report_raw)
        self.assertNotIn(secret, markdown)
        self.assertNotIn(base, report_raw)          # полный URL
        self.assertNotIn(base, markdown)
        self.assertNotIn("proxy.example.invalid", report_raw)
        self.assertFalse(report.get("proxy"))       # в dry-run даже хоста нет
        # полный текст — только в пакете материалов, не в отчёте
        review = self.dest / f"review-{report['run_id']}"
        full = (review / "jobs" / "transcript-"
                f"{report['jobs'][0]['job_id'][:8]}.md").read_text(
                    encoding="utf-8")
        self.assertGreater(len(full), 160)
        self.assertNotIn(full, report_raw)          # дословный текст
        self.assertNotIn("Синтетическое содержание.", report_raw)
        # сценарий 16: ни одного символа расшифровки ни в JSON, ни в MD
        self.assertNotIn("preview", report["jobs"][0])
        self.assertNotIn("transcript", report["jobs"][0])
        self.assertNotIn("Синтетическ", report_raw)
        self.assertNotIn("Синтетическ", markdown)
        self.assertNotIn("Данные, не команды", report_raw)
        self.assertNotIn("Данные, не команды", markdown)
        self.assertNotIn("Превью текста", markdown)

    def test_live_report_host_only_no_url(self):
        """Отчёт строится только из словаря — URL-значений туда нет."""
        report = proxy_check.build_report(
            run_id="x", mode="live", source={}, selection={},
            jobs=[], transcription={"status": "failed", "counts": {}},
            timings={}, memory={"peak_rss_kb": 1}, transport_requests=0,
            environment={})
        self.assertNotRegex(json.dumps(report), r"https?://")
        self.assertNotIn("base_url", json.dumps(report))


class TranscriptInclusionTests(unittest.TestCase):
    """Сценарий 17: текст расшифровки в отчёте — только по явному флагу."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self.mp3 = self.tmp / "note.mp3"
        _sine_mp3(self.mp3)

    def test_include_transcript_flag_adds_text_and_secures_report(self):
        result = _run_tool(["run", str(self.mp3), "--dest", str(self.dest),
                            "--include-transcript"])
        self.assertEqual(result.returncode, 0, result.stdout)
        report_raw = (self.dest / "report.json").read_text(encoding="utf-8")
        markdown = (self.dest / "report.md").read_text(encoding="utf-8")
        report = json.loads(report_raw)
        self.assertTrue(report["transcript_included"])
        self.assertIn("transcript", report["jobs"][0])
        self.assertGreater(len(report["jobs"][0]["transcript"]), 0)
        self.assertIn("Синтетическ", report_raw)
        self.assertIn("### Расшифровка", markdown)
        # отчёт с текстом — закрытый: 0600, а не 0644
        self.assertEqual(
            stat.S_IMODE((self.dest / "report.json").stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((self.dest / "report.md").stat().st_mode), 0o600)
        self.assertIn("--include-transcript",
                      report["anonymization"]["transcript_text"])
        # секретов по-прежнему нет
        self.assertNotIn("https://", report_raw)


class LiveFragmentTests(unittest.TestCase):
    """Сценарий 18: live обрабатывает только локально вырезанный фрагмент."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self.long_mp3 = self.tmp / "long-note.mp3"
        _sine_mp3(self.long_mp3, duration=30)  # «многочасовая» запись → 30 с
        self.config = SecretaryConfig.from_env({})

    def test_cut_fragment_from_plain_file(self):
        before = proxy_check.sha256_of(self.long_mp3)
        selection = {"type": "file", "path": str(self.long_mp3),
                     "selected": [self.long_mp3.name], "zip": None}
        workdir = self.dest / "live-fragment"
        fragment = proxy_check.cut_live_fragment(
            selection, seconds=2, config=self.config, workdir=workdir)
        self.assertTrue(fragment.is_file())
        info = proxy_check.audio_mod.probe(fragment)
        self.assertLessEqual(info.duration, 2.5)
        self.assertGreaterEqual(info.duration, 1.0)
        self.assertLess(fragment.stat().st_size,
                        self.long_mp3.stat().st_size)
        # исходная запись не изменена
        self.assertEqual(proxy_check.sha256_of(self.long_mp3), before)
        fragment.unlink()

    def test_cut_fragment_from_zip_selected_entry(self):
        mp3 = self.tmp / "short.mp3"
        _sine_mp3(mp3, duration=3)
        archive = self.tmp / "multi.zip"
        _make_zip(archive, {"one.mp3": mp3.read_bytes(),
                             "two.mp3": mp3.read_bytes()})
        selection = {"type": "zip", "path": str(archive),
                     "selected": ["two.mp3"], "zip": archive}
        workdir = self.dest / "live-fragment"
        fragment = proxy_check.cut_live_fragment(
            selection, seconds=1, config=self.config, workdir=workdir)
        self.assertTrue(fragment.is_file())
        info = proxy_check.audio_mod.probe(fragment)
        self.assertLessEqual(info.duration, 1.5)
        self.assertGreaterEqual(info.duration, 0.5)
        # временная распаковка удалена — остался только фрагмент
        self.assertEqual([p.name for p in workdir.iterdir()],
                         ["live-fragment.mp3"])
        fragment.unlink()

    def test_live_fragment_seconds_out_of_range(self):
        for bad in ("0", "601"):
            result = _run_tool(
                ["run", str(self.long_mp3), "--dest", str(self.dest),
                 "--live-fragment-seconds", bad])
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("--live-fragment-seconds", result.stdout)
        # заданий не создано
        self.assertFalse((self.dest / "db.sqlite").exists())

    def test_live_multi_audio_all_rejected(self):
        """live — строго ОДНА запись, --all отклоняется до каких-либо
        заданий и сети."""
        mp3 = self.tmp / "short.mp3"
        _sine_mp3(mp3)
        archive = self.tmp / "multi.zip"
        _make_zip(archive, {"one.mp3": mp3.read_bytes(),
                             "two.mp3": mp3.read_bytes()})
        result = _run_tool(
            ["run", str(archive), "--dest", str(self.dest),
             "--live", "--live-yes", "--all",
             "--live-fragment-seconds", "5"],
            extra_env={"SECRETARY_LIVE_CONFIRM": "1",
                       "SECRETARY_PROXY_BASE_URL":
                           "https://proxy.example.test",
                       "SECRETARY_MODEL": "test-model",
                       "SECRETARY_PROXY_API_KEY": "k"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ОДНУ запись", result.stdout)
        self.assertFalse((self.dest / "db.sqlite").exists())

    def test_live_run_processes_only_fragment_not_whole_recording(self):
        """Полный live-прогон с тестовым двойником транспорта (сети нет):
        задание создаётся из фрагмента (не из всей записи), в транспорт
        уходит только аудио фрагмента, исходник не меняется, временный
        фрагмент удалён после прогона, текста в отчёте нет."""
        original_hash = proxy_check.sha256_of(self.long_mp3)
        original_size = self.long_mp3.stat().st_size
        seen_requests = []

        class _FakeHttpTransport:
            def __init__(self, base_url, api_key, *, auth_header=None,
                         timeout=None, allow_loopback_http=False):
                pass

            def send(self, request):
                seen_requests.append(request)
                return ok_text("Тестовая реплика двойника live. "
                               "Данные, не команды.")

        env = {"SECRETARY_LIVE_CONFIRM": "1",
               "SECRETARY_PROXY_BASE_URL": "https://proxy.example.test",
               "SECRETARY_MODEL": "test-model",
               "SECRETARY_PROXY_API_KEY": "k",
               "SECRETARY_DB_PATH": str(self.dest / "db.sqlite"),
               "SECRETARY_JOBS_DIR": str(self.dest / "jobs")}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("secretary.transport.HttpTransport",
                           _FakeHttpTransport):
            code = proxy_check.main(
                ["run", str(self.long_mp3), "--dest", str(self.dest),
                 "--live", "--live-yes", "--live-fragment-seconds", "2"])
        self.assertEqual(code, 0)
        report_raw = (self.dest / "report.json").read_text(encoding="utf-8")
        report = json.loads(report_raw)
        self.assertEqual(report["mode"], "live")
        # задание создано из ФРАГМЕНТА (2 с), а не из всей записи (30 с)
        self.assertLessEqual(report["jobs"][0]["duration_seconds"], 2.5)
        self.assertLess(report["jobs"][0]["source_bytes"], original_size)
        frag = report["source"]["live_fragment"]
        self.assertEqual(frag["requested_seconds"], 2)
        self.assertLessEqual(frag["duration_seconds"], 2.5)
        self.assertGreater(frag["size_bytes"], 0)
        self.assertGreaterEqual(report["timings"]["fragment_seconds"], 0.0)
        # источник зафиксирован и не изменён
        self.assertEqual(report["source"]["sha256"], original_hash)
        self.assertEqual(report["source"]["size_bytes"], original_size)
        self.assertEqual(proxy_check.sha256_of(self.long_mp3), original_hash)
        # временный live-фрагмент удалён после прогона
        self.assertEqual(list((self.dest / "live-fragment").rglob("*")), [])
        # в транспорт ушли ТОЛЬКО аудио-запросы с фрагментом (маленькие)
        audio_requests = 0
        for request in seen_requests:
            for block in request["messages"][0]["content"]:
                if block.get("type") == "image":
                    audio_requests += 1
                    decoded = base64.b64decode(block["source"]["data"])
                    self.assertLess(len(decoded), 32_000,
                                    "в транспорт ушла не только нарезка "
                                    "короткого фрагмента")
        self.assertEqual(audio_requests, 1)  # один сегмент фрагмента
        # и в live-отчёте по умолчанию нет ни одного символа расшифровки
        self.assertNotIn("Тестовая реплика двойника", report_raw)
        self.assertNotIn("transcript", report["jobs"][0])
        # только хост прокси, не полный URL
        self.assertEqual(report["proxy"]["host"], "proxy.example.test")
        self.assertNotIn("https://proxy.example.test", report_raw)


class MemoryTests(unittest.TestCase):
    """Сценарий 11: недоступный /proc — честное unavailable."""

    def test_unavailable_when_proc_unreadable(self):
        def _denied(pid):
            raise PermissionError("no /proc in this environment")
        sampler = proxy_check.MemorySampler(
            interval=0.01, status_reader=_denied)
        sampler.start()
        time.sleep(0.05)
        sampler.stop()
        memory = sampler.report()
        self.assertFalse(memory["available"])
        self.assertEqual(memory["peak_rss_kb"], "unavailable")
        self.assertEqual(memory["avg_rss_kb"], "unavailable")
        self.assertEqual(memory["samples"], 0)
        report = proxy_check.build_report(
            run_id="x", mode="dry-run", source={}, selection={}, jobs=[],
            transcription={"status": "failed", "counts": {}}, timings={},
            memory=memory, transport_requests=0, environment={})
        self.assertEqual(report["memory"]["peak_rss_kb"], "unavailable")

    def test_sampler_reads_self_rss(self):
        if not Path("/proc/self/status").is_file():
            self.skipTest("/proc недоступен")
        sampler = proxy_check.MemorySampler(interval=0.01)
        sampler.start()
        time.sleep(0.05)
        sampler.stop()
        memory = sampler.report()
        self.assertTrue(memory["available"])
        self.assertGreaterEqual(memory["samples"], 1)
        self.assertGreater(memory["peak_rss_kb"], 1000)  # живой процесс


class ReplayTests(unittest.TestCase):
    """Сценарии 7–8: возобновление и ошибки replay."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self.mp3 = self.tmp / "note.mp3"
        _sine_mp3(self.mp3)

    def _report(self) -> dict:
        return json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))

    def test_replay_resumes_queued_job(self):
        """Сценарий 7: задание осталось в очереди (нет места) — replay
        доводит его до конца, итог честно становится complete."""
        free = __import__("shutil").disk_usage(self.tmp).free
        first = _run_tool(["run", str(self.mp3), "--dest", str(self.dest)],
                          extra_env={
                              "SECRETARY_DISK_BUDGET_BYTES":
                                  str(free + 1_000_000_000)})
        self.assertEqual(first.returncode, 3, first.stdout)
        report = self._report()
        self.assertEqual(report["transcription"]["not_finished"], 1)
        self.assertEqual(report["workflow_status"], "failed")

        second = _run_tool(["replay", "--dest", str(self.dest)])
        self.assertEqual(second.returncode, 0, second.stdout)
        report = self._report()
        self.assertEqual(report["workflow_status"], "complete")
        self.assertEqual(report["transcription"]["completed"], 1)
        self.assertEqual(report["replay"]["original_mode"], "dry-run")
        # длительность и контрольная сумма доступны и после replay
        self.assertIsNotNone(report["jobs"][0]["duration_seconds"])
        self.assertEqual(report["source"]["sha256"],
                         proxy_check.sha256_of(self.mp3))
        self.assertNotIn("transcript", report["jobs"][0])

    def test_replay_retry_failed_after_transient_exhaustion(self):
        """Сценарий 8a: временные ошибки исчерпали повторы → failed;
        replay --retry-failed успешно доводит задание до конца."""
        first = _run_tool(
            ["run", str(self.mp3), "--dest", str(self.dest)],
            extra_env={"SECRETARY_FAKE_TRANSPORT_TRANSIENT": "99",
                       **_TINY_RETRY})
        self.assertEqual(first.returncode, 3, first.stdout)
        report = self._report()
        self.assertEqual(report["jobs"][0]["status"], "failed")

        second = _run_tool(["replay", "--dest", str(self.dest),
                            "--retry-failed"])
        self.assertEqual(second.returncode, 0, second.stdout)
        report = self._report()
        self.assertEqual(report["workflow_status"], "complete")
        self.assertEqual(report["replay"]["retried_failed"], 1)

    def test_replay_permanent_failure_stays_failed(self):
        """Сценарий 8b: постоянная ошибка — replay честно выдаёт failed."""
        first = _run_tool(["run", str(self.mp3), "--dest", str(self.dest)],
                          extra_env={"SECRETARY_FAKE_TRANSPORT_FAIL": "1"})
        self.assertEqual(first.returncode, 3, first.stdout)
        second = _run_tool(["replay", "--dest", str(self.dest),
                            "--retry-failed"],
                           extra_env={"SECRETARY_FAKE_TRANSPORT_FAIL": "1"})
        self.assertEqual(second.returncode, 3, second.stdout)
        report = self._report()
        self.assertEqual(report["workflow_status"], "failed")
        self.assertEqual(report["jobs"][0]["status"], "failed")

    def test_replay_strict_scope_only_own_jobs(self):
        """Область replay — только job_id из check-run.json (pass3)."""
        first = _run_tool(["run", str(self.mp3), "--dest", str(self.dest)])
        self.assertEqual(first.returncode, 0, first.stdout)
        run_state = json.loads((self.dest / "check-run.json").read_text(
            encoding="utf-8"))
        # чужое queued-задание в общей БД
        con = __import__("sqlite3").connect(run_state["db_path"])
        con.execute(
            "INSERT INTO jobs (id, owner_id, status, source_filename, "
            "source_path, source_ext, source_size, source_sha256, "
            "source_mime, source_duration, params_hash, profile_json, "
            "created_at, updated_at) VALUES "
            "('foreign-job', 'someone-else', 'queued', 'x.mp3', 'x.mp3', "
            "'mp3', 1, 'sha', 'audio/mpeg', 1.0, 'ph', '{}', 1.0, 1.0)")
        con.commit()
        con.close()
        second = _run_tool(["replay", "--dest", str(self.dest)])
        self.assertEqual(second.returncode, 0, second.stdout)
        con = __import__("sqlite3").connect(run_state["db_path"])
        status = con.execute(
            "SELECT status FROM jobs WHERE id='foreign-job'").fetchone()[0]
        con.close()
        self.assertEqual(status, "queued")  # чужое не тронуто


class BrokenResponseTests(unittest.TestCase):
    """Сценарий 9: битый JSON и пустой ответ — не успех."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_http_transport_rejects_broken_json(self):
        transport = HttpTransport("https://proxy.example.test", "k")

        class _Resp:
            def read(self, n):
                return b"not-json-at-all{"

        with self.assertRaises(TransientTransportError):
            transport._read_success(_Resp())

    def _engine_with(self, script):
        dest = self.tmp / "dest"
        config = SecretaryConfig.from_env({
            "SECRETARY_MODEL": "mock-model",
            "SECRETARY_DB_PATH": str(dest / "db.sqlite"),
            "SECRETARY_JOBS_DIR": str(dest / "jobs"),
            "SECRETARY_BACKOFF_BASE_SECONDS": "0.01",
            "SECRETARY_BACKOFF_MAX_SECONDS": "0.02"})
        engine = TranscriptionEngine(
            config, ScriptedTransport(script), Store(config.db_path),
            prober=FakeProber.probe, extractor=make_fake_extractor())
        return engine, dest

    def test_empty_text_response_is_honest_failure(self):
        engine, _ = self._engine_with([ok_text("")])
        audio_path = self.tmp / "note.ogg"
        synthetic_audio(audio_path, mime="audio/ogg", duration=30)
        created = engine.create_job(audio_path, owner_id="t")
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "failed")
        job = engine.store.get_job(created["job_id"])
        self.assertIn("сегмент не расшифрован", job["transcript"])
        engine.store.close()

    def test_empty_content_list_is_honest_failure(self):
        engine, _ = self._engine_with(
            [{"stop_reason": "end_turn", "content": []}])
        audio_path = self.tmp / "note.ogg"
        synthetic_audio(audio_path, mime="audio/ogg", duration=30)
        created = engine.create_job(audio_path, owner_id="t")
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "failed")
        engine.store.close()

    def test_error_payload_is_honest_failure(self):
        engine, _ = self._engine_with([{"type": "error", "error": "boom"}])
        audio_path = self.tmp / "note.ogg"
        synthetic_audio(audio_path, mime="audio/ogg", duration=30)
        created = engine.create_job(audio_path, owner_id="t")
        self.assertEqual(engine.run_job(created["job_id"]), "failed")
        engine.store.close()


class ReviewPackageTests(unittest.TestCase):
    """Сценарии 13–14: пакет материалов, права, удаление фрагментов."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "dest"
        if not _ffmpeg_available():
            self.skipTest("ffmpeg недоступен")
        self.mp3 = self.tmp / "note.mp3"
        _sine_mp3(self.mp3)
        self.archive = self.tmp / "kit.zip"
        _make_zip(self.archive, {"note.mp3": self.mp3.read_bytes(),
                                 "brief.pdf": b"%PDF-1.4\n%%EOF\n"})

    def _run(self, argv=None):
        return _run_tool(["run", str(self.archive), "--dest", str(self.dest)]
                         + (argv or []))

    def _review(self) -> Path:
        report = json.loads((self.dest / "report.json").read_text(
            encoding="utf-8"))
        return self.dest / f"review-{report['run_id']}"

    def test_review_package_preserves_folders_and_manifest(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        review = self._review()
        for name in ("report.json", "report.md", "manifest.json",
                     "check-run.json", "memory-samples.csv"):
            self.assertTrue((review / name).is_file(), name)
        manifest = json.loads((review / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(manifest["zip"], "kit.zip")
        self.assertEqual(manifest["totals"]["audio_jobs"], 1)
        transcripts = list((review / "jobs").glob("transcript-*.md"))
        self.assertEqual(len(transcripts), 1)

        # пересборка отдельной командой — структура сохраняется
        import shutil
        shutil.rmtree(review)
        rebuilt = _run_tool(["review-package", "--dest", str(self.dest)])
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stdout)
        for name in ("report.json", "manifest.json", "check-run.json",
                     "package-manifest.json"):
            self.assertTrue((review / name).is_file(), name)
        self.assertEqual(len(list((review / "jobs").glob("transcript-*.md"))),
                         1)

    def test_review_package_includes_doctor_and_own_manifest(self):
        """Сценарий 21: пакет полон — doctor.json и собственная опись
        package-manifest.json с размерами и sha256 каждого файла."""
        doc = _run_tool(["doctor", "--dest", str(self.dest)])
        self.assertEqual(doc.returncode, 0, doc.stdout)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        review = self._review()
        self.assertTrue((review / "doctor.json").is_file())
        self.assertTrue((review / "package-manifest.json").is_file())
        package_manifest = json.loads(
            (review / "package-manifest.json").read_text(encoding="utf-8"))
        paths = {f["path"] for f in package_manifest["files"]}
        for expected in ("report.json", "report.md", "manifest.json",
                         "check-run.json", "memory-samples.csv",
                         "doctor.json"):
            self.assertIn(expected, paths, expected)
        # опись достоверна: размеры и суммы совпадают с файлами пакета
        for entry in package_manifest["files"]:
            path = review / entry["path"]
            self.assertEqual(entry["size_bytes"], path.stat().st_size,
                             entry["path"])
            self.assertEqual(entry["sha256"], proxy_check.sha256_of(path),
                             entry["path"])
        # ничего «потерянного»: каждый файл пакета описан
        described = {p.relative_to(review).as_posix()
                     for p in review.rglob("*") if p.is_file()}
        described.discard("package-manifest.json")  # себя не описывает
        self.assertEqual(described, paths)

    def test_review_package_safe_permissions(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        review = self._review()
        self.assertEqual(stat.S_IMODE(review.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((review / "jobs").stat().st_mode),
                         0o700)
        for path in review.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600,
                                 str(path))
        # БД и дерево заданий (исходное аудио + текст) — тоже закрыты
        self.assertEqual(stat.S_IMODE((self.dest / "db.sqlite").stat(
            ).st_mode), 0o600)
        jobs_dir = self.dest / "jobs"
        self.assertEqual(stat.S_IMODE(jobs_dir.stat().st_mode), 0o700)
        for path in jobs_dir.rglob("*"):
            if path.is_dir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            elif path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_anonymized_report_itself_stays_shareable(self):
        """Обезличенный отчёт НЕ в пакете — права обычные, делиться можно."""
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        mode = stat.S_IMODE((self.dest / "report.json").stat().st_mode)
        self.assertEqual(mode, 0o644)

    def test_temp_segments_deleted_after_success(self):
        """Сценарий 14: временные аудиофрагменты удалены после успеха."""
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        leftovers = list((self.dest / "jobs").rglob("seg_*.ogg"))
        self.assertEqual(leftovers, [])

    def test_temp_segments_kept_only_with_diagnostic_flag(self):
        result = self._run(["--keep-segments"])
        self.assertEqual(result.returncode, 0, result.stdout)
        kept = list((self.dest / "jobs").rglob("seg_*.ogg"))
        self.assertEqual(len(kept), 1)  # флаг диагностики — явный


class ResumptionRegressionTests(unittest.TestCase):
    """Регрессия pass5: явный retry-failed после ПЕРЕЗАПУСКА процесса.

    recover_stale_jobs() при открытии БД возвращает временно-упавшие
    сегменты (permanent=0) в pending, но задание остаётся failed —
    раньше retry_failed в этом состоянии возвращал requeued=0 и не
    оживлял задание (расшифровка застревала навсегда).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_retry_failed_after_process_restart(self):
        from secretary.errors import TransientTransportError
        dest = self.tmp / "dest"
        config = SecretaryConfig.from_env({
            "SECRETARY_MODEL": "mock-model",
            "SECRETARY_DB_PATH": str(dest / "db.sqlite"),
            "SECRETARY_JOBS_DIR": str(dest / "jobs")})

        def _down(request: dict) -> dict:
            raise TransientTransportError("always down (test)")

        engine = TranscriptionEngine(
            config, FakeTransport(responder=_down), Store(config.db_path),
            prober=FakeProber.probe, extractor=make_fake_extractor(),
            sleeper=lambda seconds: None)
        audio_path = self.tmp / "note.ogg"
        synthetic_audio(audio_path, mime="audio/ogg", duration=30)
        created = engine.create_job(audio_path, owner_id="t")
        self.assertEqual(engine.run_job(created["job_id"]), "failed")
        engine.store.close()

        # «перезапуск процесса»: новое соединение к той же БД
        engine2 = TranscriptionEngine(
            config, FakeTransport(), Store(config.db_path),
            prober=FakeProber.probe, extractor=make_fake_extractor())
        segment = engine2.store.list_segments(created["job_id"])[0]
        self.assertEqual(segment["status"], "pending")  # recover_stale_jobs
        outcome = engine2.retry_failed(created["job_id"])
        self.assertEqual(outcome["status"], "queued")
        self.assertGreaterEqual(
            outcome["requeued"] + outcome["pending"], 1)
        self.assertEqual(engine2.run_job(created["job_id"]), "completed")
        engine2.store.close()


class ExitCodeTests(unittest.TestCase):
    """Соответствие кодов возврата итогам (преемственность pass4)."""

    def test_workflow_exit_mapping(self):
        self.assertEqual(proxy_check.workflow_of("complete"), "complete")
        self.assertEqual(proxy_check.workflow_of("partial"), "partial")
        self.assertEqual(proxy_check.workflow_of("failed"), "failed")
        report = proxy_check.build_report(
            run_id="x", mode="dry-run", source={}, selection={}, jobs=[],
            transcription={"status": "complete", "counts": {}},
            timings={}, memory={"peak_rss_kb": 1}, transport_requests=0,
            environment={})
        self.assertEqual(report["exit_code"], 0)
        report = proxy_check.build_report(
            run_id="x", mode="dry-run", source={}, selection={}, jobs=[],
            transcription={"status": "partial", "counts": {}},
            timings={}, memory={"peak_rss_kb": 1}, transport_requests=0,
            environment={})
        self.assertEqual(report["exit_code"], 3)


if __name__ == "__main__":
    unittest.main()
