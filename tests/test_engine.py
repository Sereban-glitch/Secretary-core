"""Офлайн-тесты движка: полный цикл на фейковом транспорте и аудио."""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from secretary.config import SecretaryConfig
from secretary.engine import TranscriptionEngine
from secretary.errors import (AudioError, PermanentTransportError,
                              TransientTransportError, ValidationError)
from secretary.store import Store
from tests.fakes import (FakeProber, FakeTransport, default_draft,
                          make_fake_extractor, ok_text, synthetic_audio)

SECRET = "SECRET-KEY-42"


def make_config(tmp: str, **overrides) -> SecretaryConfig:
    values = dict(
        model="mock-model",
        segment_seconds=30.0,
        overlap_seconds=4.0,
        min_segment_seconds=10.0,
        max_audio_bytes=10 * 1024 * 1024,
        max_source_bytes=10 * 1024 * 1024,
        db_path=str(Path(tmp) / "db.sqlite"),
        jobs_dir=str(Path(tmp) / "jobs"),
        max_attempts=3,
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
        retry_after_cap_seconds=0.01,
        structure_chunk_chars=400,
        proxy_api_key=SECRET,
    )
    values.update(overrides)
    return SecretaryConfig(**values)


def make_engine(tmp: str, responder=None, **cfg_overrides):
    config = make_config(tmp, **cfg_overrides)
    transport = FakeTransport(responder)
    store = Store(config.db_path)
    engine = TranscriptionEngine(config, transport, store,
                                 prober=FakeProber.probe,
                                 extractor=make_fake_extractor())
    return engine, transport


class EngineBasics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.audio = synthetic_audio(Path(self.tmp) / "audio-61.ogg",
                                     mime="audio/ogg", duration=61)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_and_run_completed(self):
        engine, transport = make_engine(self.tmp)
        created = engine.create_job(self.audio)
        self.assertTrue(created["created"])
        self.assertEqual(created["segments_total"], 2)  # 0-30, 26-61
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "completed")
        job = engine.store.get_job(created["job_id"])
        self.assertIn("[SEG 0]", job["transcript"])
        self.assertIn("[SEG 1]", job["transcript"])
        self.assertIn("измеренные границы нарезки", job["transcript"])
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual([s["status"] for s in segments], ["done", "done"])
        # warnings_json сегмента — список, а не строка (регрессия CLI smoke)
        self.assertEqual(segments[0]["warnings_json"], [])
        self.assertIsNotNone(job["structured_json"])
        draft = job["structured_json"]  # store парсит JSON при чтении
        self.assertIn("summary", draft)
        # provenance сохранён и не содержит секрет
        self.assertEqual(job["profile_json"]["model"], "mock-model")
        self.assertNotIn(SECRET, json.dumps(job["profile_json"]))
        self.assertEqual(job["profile_json"]["transport"], "FakeTransport")

    def test_segment_plan_boundaries(self):
        engine, _ = make_engine(self.tmp)
        created = engine.create_job(self.audio)
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 30.0)
        self.assertEqual(segments[1]["start"], 26.0)  # перекрытие 4 с
        self.assertEqual(segments[1]["end"], 61.0)

    def test_vocabulary_and_language_in_prompt(self):
        engine, transport = make_engine(self.tmp)
        engine.create_job(self.audio, language="uk",
                           vocabulary=["Опрыскувач", "Berthoud"])
        engine.run_pending(max_jobs=1)
        prompt = FakeTransport.prompt_text(transport.audio_requests[0])
        self.assertIn("Основной язык записи: uk", prompt)
        self.assertIn("Berthoud", prompt)
        self.assertIn("Опрыскувач", prompt)

    def test_seam_dedup(self):
        texts = {
            (0.0, 30.0): "Первая фраза сегмента.\nВторая фраза.\nТретья фраза.",
            (26.0, 61.0): "Вторая фраза.\nТретья фраза.\nХвост второго сегмента.",
        }

        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker:
                return ok_text(texts[marker])
            return ok_text(json.dumps(default_draft("Черновик.", [0, 1])))

        engine, _ = make_engine(self.tmp, responder)
        created = engine.create_job(self.audio)
        engine.run_job(created["job_id"])
        job = engine.store.get_job(created["job_id"])
        transcript = job["transcript"]
        # дублированные строки стыка удалены
        self.assertEqual(transcript.count("Вторая фраза."), 1)
        self.assertEqual(transcript.count("Третья фраза."), 1)
        self.assertIn("Хвост второго сегмента.", transcript)
        self.assertTrue(any(w.startswith("overlap_dedup")
                            for w in job["warnings_json"]))

    def test_partial_failure_permanent(self):
        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker and marker[0] >= 26:
                raise PermanentTransportError("model refused audio")
            if marker:
                return ok_text("Текст первого сегмента.")
            return ok_text(json.dumps(default_draft("Черновик.", [0])))

        engine, _ = make_engine(self.tmp, responder)
        created = engine.create_job(self.audio)
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "partial")
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual(segments[0]["status"], "done")
        self.assertEqual(segments[1]["status"], "failed")
        self.assertEqual(segments[1]["permanent"], 1)
        job = engine.store.get_job(created["job_id"])
        self.assertIn("[сегмент не расшифрован", job["transcript"])
        self.assertTrue(any(w.startswith("failed_segments")
                            for w in job["warnings_json"]))

    def test_transient_retries_then_success(self):
        state = {"fails": 0}

        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker:
                if marker[0] == 0.0:  # флейчит только первый сегмент
                    state["fails"] += 1
                    if state["fails"] <= 2:
                        raise TransientTransportError("temporary glitch")
                return ok_text(f"Текст сегмента {marker[0]:.0f} после повторов.")
            return ok_text(json.dumps(default_draft("Черновик.", [0, 1])))

        engine, _ = make_engine(self.tmp, responder)
        created = engine.create_job(self.audio)
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "completed")
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual(segments[0]["attempts"], 3)
        self.assertEqual(segments[1]["attempts"], 1)

    def test_transient_exhausted_then_retry_failed(self):
        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker and marker[0] >= 26:
                raise TransientTransportError("always down")
            if marker:
                return ok_text("Текст сегмента один.")
            return ok_text(json.dumps(default_draft("Черновик.", [0])))

        engine, transport = make_engine(self.tmp, responder)
        created = engine.create_job(self.audio)
        self.assertEqual(engine.run_job(created["job_id"]), "partial")
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual(segments[1]["permanent"], 0)  # временная причина

        # транспорт «починился»: явный retry-failed и повторный прогон
        transport._responder = FakeTransport()._default_responder
        engine.retry_failed(created["job_id"])
        self.assertEqual(engine.run_job(created["job_id"]), "completed")

    def test_cancel_mid_job_and_resume(self):
        holder = {}

        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker and marker[0] < 26:
                holder["engine"].cancel(holder["job_id"])  # отмена во время 0-го
                return ok_text("Текст сегмента ноль.")
            if marker:
                return ok_text("Текст сегмента один.")
            return ok_text(json.dumps(default_draft("Черновик.", [0, 1])))

        engine, transport = make_engine(self.tmp, responder)
        holder["engine"] = engine
        created = engine.create_job(self.audio)
        holder["job_id"] = created["job_id"]
        self.assertEqual(engine.run_job(created["job_id"]), "cancelled")
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual(segments[0]["status"], "done")
        self.assertEqual(segments[1]["status"], "pending")

        # перезапуск продолжает незавершённые сегменты
        transport._responder = FakeTransport()._default_responder
        engine.retry_failed(created["job_id"])
        self.assertEqual(engine.run_job(created["job_id"]), "completed")

    def test_empty_response_is_permanent_failure(self):
        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker and marker[0] >= 26:
                return {"stop_reason": "max_tokens",
                        "content": [{"type": "text", "text": "обрыв"}]}
            if marker:
                return ok_text("Текст сегмента ноль.")
            return ok_text(json.dumps(default_draft("Черновик.", [0])))

        engine, _ = make_engine(self.tmp, responder)
        created = engine.create_job(self.audio)
        self.assertEqual(engine.run_job(created["job_id"]), "partial")
        segments = engine.store.list_segments(created["job_id"])
        self.assertEqual(segments[1]["permanent"], 1)
        self.assertIn("invalid response", segments[1]["error"])

    def test_oversized_segment_split(self):
        # размер сегмента = 4 байта/с; лимит 100 байт → деление до ≤25 с
        config_overrides = {"max_audio_bytes": 100}
        engine, _ = make_engine(self.tmp, None, **config_overrides)
        long_audio = synthetic_audio(Path(self.tmp) / "audio-120.ogg",
                                     mime="audio/ogg", duration=120)
        created = engine.create_job(long_audio)
        self.assertEqual(engine.run_job(created["job_id"]), "completed")
        segments = engine.store.list_segments(created["job_id"])
        self.assertGreater(len(segments), 2)  # было 2 сегмента по 60 с
        for s in segments:
            self.assertLessEqual((s["end"] - s["start"]) * 4, 100 + 8)
        job = engine.store.get_job(created["job_id"])
        self.assertTrue(any("split" in w for w in job["warnings_json"]))

    def test_structure_failure_keeps_transcript(self):
        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker:
                return ok_text("Транскрипт сохранён полностью.")
            raise PermanentTransportError("structure endpoint broken")

        engine, _ = make_engine(self.tmp, responder)
        created = engine.create_job(self.audio)
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "completed")
        job = engine.store.get_job(created["job_id"])
        self.assertIsNone(job["structured_json"])
        self.assertIn("Транскрипт сохранён полностью.", job["transcript"])
        self.assertTrue(any(w.startswith("structuring_failed")
                            for w in job["warnings_json"]))

    def test_structure_chunking_with_model_merge(self):
        merged_calls = []

        def responder(request):
            marker = FakeTransport.decode_marker(request)
            if marker:
                return ok_text("Длинный синтетический текст сегмента. " * 12)
            prompt = FakeTransport.prompt_text(request)
            if "объедин" in prompt.lower():
                merged_calls.append(1)
                return ok_text(json.dumps(
                    default_draft("Объединённый черновик.", [0, 1])))
            return ok_text(json.dumps({
                "summary": "Часть.", "topics": [], "statements": [],
                "questionable_places": [], "speakers_note": ""}))

        engine, _ = make_engine(self.tmp, responder,
                                structure_chunk_chars=300)
        created = engine.create_job(self.audio)
        engine.run_job(created["job_id"])
        job = engine.store.get_job(created["job_id"])
        draft = job["structured_json"]
        self.assertEqual(draft["summary"], "Объединённый черновик.")
        self.assertGreater(len(merged_calls), 0)

    def test_idempotency_considers_params_and_owner(self):
        engine, _ = make_engine(self.tmp)
        first = engine.create_job(self.audio)
        same = engine.create_job(self.audio)
        self.assertEqual(first["job_id"], same["job_id"])
        self.assertFalse(same["created"])
        with_vocab = engine.create_job(self.audio, vocabulary=["Опрыскувач"])
        self.assertNotEqual(first["job_id"], with_vocab["job_id"])
        other_owner = engine.create_job(self.audio, owner_id="natalia")
        self.assertNotEqual(first["job_id"], other_owner["job_id"])

    def test_two_jobs_not_mixed(self):
        engine, _ = make_engine(self.tmp)
        other = synthetic_audio(Path(self.tmp) / "audio-40.wav",
                                mime="audio/wav", duration=40)
        first = engine.create_job(self.audio, owner_id="a")
        second = engine.create_job(other, owner_id="b")
        engine.run_pending()
        for job_id, expected in ((first["job_id"], "61"), (second["job_id"], "40")):
            job = engine.store.get_job(job_id)
            segments = engine.store.list_segments(job_id)
            texts = [s["text"] for s in segments]
            self.assertTrue(any(expected in t for t in texts),
                            f"{expected} отсутствует в {texts}")
            self.assertEqual(job["source_filename"],
                             "audio-61.ogg" if expected == "61" else "audio-40.wav")
        # отдельные папки заданий
        jobs_dir = Path(engine.config.jobs_dir)
        self.assertTrue((jobs_dir / first["job_id"] / "source.ogg").is_file())
        self.assertTrue((jobs_dir / second["job_id"] / "source.wav").is_file())

    def test_recovery_after_crash(self):
        engine, _ = make_engine(self.tmp)
        other = synthetic_audio(Path(self.tmp) / "audio-40.wav",
                                mime="audio/wav", duration=40)
        first = engine.create_job(self.audio)
        second = engine.create_job(other)
        # имитация падения воркера: running-задача, running-сегмент
        engine.store.update_job(first["job_id"], status="running")
        engine.store.update_segment(first["job_id"], 0, status="running")
        # новый Store (перезапуск) выполняет восстановление
        store2 = Store(engine.config.db_path)
        job = store2.get_job(first["job_id"])
        self.assertEqual(job["status"], "queued")
        seg = store2.list_segments(first["job_id"])[0]
        self.assertEqual(seg["status"], "pending")
        engine.run_pending()
        for job_id in (first["job_id"], second["job_id"]):
            self.assertEqual(
                engine.store.get_job(job_id)["status"], "completed")

    def test_missing_source_fails(self):
        engine, _ = make_engine(self.tmp)
        created = engine.create_job(self.audio)
        (Path(engine.config.jobs_dir) / created["job_id"] /
         "source.ogg").unlink()
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "failed")
        self.assertIn("source file missing",
                      engine.store.get_job(created["job_id"])["error"])

    def test_input_validation(self):
        import dataclasses
        engine, _ = make_engine(self.tmp)
        # неверное расширение — отклоняется аудио-слоем
        bad_ext = Path(self.tmp) / "voice-30.flac"
        bad_ext.write_bytes(b"OggS" + b"\x00" * 64)
        with self.assertRaises(AudioError):
            engine.create_job(bad_ext)
        # содержимое не совпадает с расширением
        mismatch = Path(self.tmp) / "voice-30.ogg"
        mismatch.write_bytes(b"ID3" + b"\x00" * 64)
        with self.assertRaises(AudioError):
            engine.create_job(mismatch)
        # длительность сверх лимита (конфиг immutable → replace)
        engine.config = dataclasses.replace(engine.config,
                                            max_duration_seconds=60)
        with self.assertRaises(ValidationError):
            engine.create_job(self.audio)  # 61 с
        # превышение размера исходника
        engine.config = dataclasses.replace(engine.config,
                                            max_duration_seconds=43200,
                                            max_source_bytes=10)
        with self.assertRaises(ValidationError):
            engine.create_job(self.audio)

    def test_no_secrets_in_logs(self):
        logs = []

        class Capture(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        logger = logging.getLogger("secretary")
        handler = Capture()
        logger.addHandler(handler)
        try:
            logger.setLevel(logging.INFO)
            engine, _ = make_engine(self.tmp)
            created = engine.create_job(self.audio)
            engine.run_job(created["job_id"])
        finally:
            logger.removeHandler(handler)
        for message in logs:
            self.assertNotIn(SECRET, message)

    def test_progress(self):
        engine, _ = make_engine(self.tmp)
        created = engine.create_job(self.audio)
        progress = engine.progress(created["job_id"])
        self.assertEqual(progress["status"], "queued")
        self.assertEqual(progress["segments"]["total"], 2)
        self.assertEqual(progress["segments"]["pending"], 2)
        engine.run_job(created["job_id"])
        progress = engine.progress(created["job_id"])
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["segments"]["done"], 2)


class CreateJobRobustnessTests(unittest.TestCase):
    """pass2, P2: сбой между записью SQLite и копированием исходника."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self.tmp_path = Path(self._tmp.name)
        self.audio = synthetic_audio(self.tmp_path / "audio-61.ogg",
                                     mime="audio/ogg", duration=61)

    def tearDown(self):
        self._tmp.cleanup()

    def test_copy_failure_leaves_no_db_row(self):
        """Сбой копирования ДО записи в БД: строки нет — повтор создаёт задачу."""
        import unittest.mock
        engine, _ = make_engine(str(self.tmp))
        with unittest.mock.patch.object(
                TranscriptionEngine, "_place_source",
                side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                engine.create_job(self.audio)
        # ни строки БД, ни «сломанного дубликата»
        self.assertEqual(engine.store.list_jobs(), [])
        # пустых job-директорий не осталось
        jobs_dir = Path(engine.jobs_dir)
        if jobs_dir.exists():
            self.assertEqual([p for p in jobs_dir.iterdir()], [])
        # повторная попытка (копирование снова работает) — задача создаётся
        created = engine.create_job(self.audio)
        self.assertTrue(created["created"])

    def test_crash_between_db_and_rename_recovered_lazily(self):
        """Крах между INSERT и rename: run_job ленивно чинит source."""
        engine, _ = make_engine(str(self.tmp))
        # воспроизводим состояние вручную: строка БД + staging-файл,
        # финального source.ogg нет (процесс умер между шагами 2 и 3)
        created = engine.create_job(self.audio)
        job_id = created["job_id"]
        job_dir = Path(engine.jobs_dir) / job_id
        source = job_dir / "source.ogg"
        staged = job_dir / ".source.ogg.tmp"
        import shutil as _shutil
        _shutil.move(str(source), str(staged))
        # выполняем очередь: источник восстанавливается, задание completed
        stats = engine.run_pending()
        self.assertEqual(stats["processed"], 1)
        job = engine.store.get_job(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(source.is_file())
        self.assertFalse(staged.exists())

    def test_missing_source_without_staging_fails(self):
        """Файл утрачен бесследно (нет staging) — честный failed, не копание."""
        engine, _ = make_engine(str(self.tmp))
        created = engine.create_job(self.audio)
        job_dir = Path(engine.jobs_dir) / created["job_id"]
        for path in job_dir.iterdir():
            path.unlink()
        status = engine.run_job(created["job_id"])
        self.assertEqual(status, "failed")
        job = engine.store.get_job(created["job_id"])
        self.assertIn("missing", job["error"])

    def test_move_source_replaces_without_copy(self):
        """move_source=True: файл переносится (не копируется) в job-директорию."""
        import shutil as _shutil
        engine, _ = make_engine(str(self.tmp))
        incoming = self.tmp_path / "incoming.ogg"
        _shutil.copy2(self.audio, incoming)
        created = engine.create_job(incoming, move_source=True)
        self.assertTrue(created["created"])
        self.assertFalse(incoming.exists())  # перенесён, не скопирован
        job = engine.store.get_job(created["job_id"])
        src = Path(engine.jobs_dir) / created["job_id"] / job["source_path"]
        self.assertEqual(src.read_bytes(), self.audio.read_bytes())

    def test_duplicate_after_move_source(self):
        """Идемпотентность сохраняется при move_source (дубликат → 200/False)."""
        import shutil as _shutil
        engine, _ = make_engine(str(self.tmp))
        first = engine.create_job(self.audio)
        incoming = self.tmp_path / "incoming.ogg"
        _shutil.copy2(self.audio, incoming)
        second = engine.create_job(incoming, move_source=True)
        self.assertFalse(second["created"])
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertFalse(incoming.exists())  # staging убран
        self.assertEqual(len(engine.store.list_jobs()), 1)


class RunPendingScopeTests(unittest.TestCase):
    """run_pending(job_ids=...) — область действия одного импорта.

    Ревью pass3, P1 «смешение импортов»: оркестратор передаёт точные
    job_id текущего ZIP; чужие/старые задания общей БД не трогаются.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self.tmp_path = Path(self._tmp.name)
        self.audios = [
            synthetic_audio(self.tmp_path / f"audio-{i:02d}.ogg",
                            mime="audio/ogg", duration=61,
                            extra=f"uniq-{i}".encode())
            for i in range(3)]

    def tearDown(self):
        self._tmp.cleanup()

    def _create(self, engine, index, owner="someone-else"):
        return engine.create_job(self.audios[index],
                                 filename=self.audios[index].name,
                                 owner_id=owner)

    def test_scoped_run_leaves_foreign_queued_untouched(self):
        engine, _ = make_engine(str(self.tmp))
        foreign = self._create(engine, 0, owner="someone-else")
        mine = self._create(engine, 1, owner="orchestrator-run-1")
        stats = engine.run_pending(job_ids=[mine["job_id"]])
        self.assertEqual(stats["scope"], "job_ids")
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(engine.store.get_job(mine["job_id"])["status"],
                         "completed")
        # чужое задание осталось в очереди, хотя оно «queued»
        self.assertEqual(engine.store.get_job(foreign["job_id"])["status"],
                         "queued")

    def test_empty_scope_processes_nothing(self):
        engine, _ = make_engine(str(self.tmp))
        foreign = self._create(engine, 0)
        stats = engine.run_pending(job_ids=[])
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(engine.store.get_job(foreign["job_id"])["status"],
                         "queued")

    def test_terminal_job_counted_as_skipped(self):
        engine, _ = make_engine(str(self.tmp))
        done = self._create(engine, 0, owner="orchestrator-run-1")
        engine.run_job(done["job_id"])
        self.assertEqual(engine.store.get_job(done["job_id"])["status"],
                         "completed")
        stats = engine.run_pending(job_ids=[done["job_id"]])
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(stats["skipped_terminal"], 1)
        self.assertEqual(engine.store.get_job(done["job_id"])["status"],
                         "completed")  # не перезапущен повторно

    def test_missing_job_reported_not_raised(self):
        engine, _ = make_engine(str(self.tmp))
        mine = self._create(engine, 0, owner="orchestrator-run-1")
        stats = engine.run_pending(job_ids=[mine["job_id"], "deadbeef"])
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["missing"], 1)

    def test_queue_mode_unchanged(self):
        """job_ids=None — прежняя семантика общей очереди (CLI-воркер)."""
        engine, _ = make_engine(str(self.tmp))
        first = self._create(engine, 0, owner="cli")
        second = self._create(engine, 1, owner="cli")
        stats = engine.run_pending()
        self.assertEqual(stats["scope"], "queue")
        self.assertEqual(stats["processed"], 2)
        self.assertEqual(engine.store.get_job(first["job_id"])["status"],
                         "completed")
        self.assertEqual(engine.store.get_job(second["job_id"])["status"],
                         "completed")


if __name__ == "__main__":
    unittest.main()
