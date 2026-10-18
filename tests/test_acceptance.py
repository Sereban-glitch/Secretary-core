"""Приёмочный сценарий на синтетическом архиве владельца.

Реальный приёмочный архив владельца «Хорш.zip» (в публичный репозиторий не
включён) имеет ТОЧНЫЙ состав (подтверждён ревью pass2):
- 3 документа: PDF + PPTX + DOCX;
- 5 аудио: 4 MP3 + 1 M4A.
Здесь состав воспроизведён синтетически БЕЗ подмены набора (в отличие от
прошлой версии теста, где PPTX отсутствовал и отклонялся).

Конвейер настоящий (реальные ffmpeg/ffprobe, реальная сегментация,
настоящее хранилище), транспорт — детерминированный fake (живой прокси
недоступен; отмечено в HANDOFF.md). Статус completed — только при
полном покрытии сегментов.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from secretary import audio
from secretary.engine import TranscriptionEngine
from secretary.store import Store
from secretary.zipimport import import_zip
from tests.fakes import FakeTransport
from tests.minijsonschema import validate
from tests.test_engine import make_config

FFMPEG = shutil.which("ffmpeg") is not None


def make_sine(path: Path, duration: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={440 + hash(path.name) % 200}:"
                              f"duration={duration}",
         str(path)], check=True, capture_output=True)
    return path


@unittest.skipUnless(FFMPEG, "ffmpeg недоступен")
class AcceptanceArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        config = make_config(self.tmp, segment_seconds=3.0,
                             overlap_seconds=0.5, min_segment_seconds=1.0)
        self.transport = FakeTransport()
        self.store = Store(config.db_path)
        self.engine = TranscriptionEngine(config, self.transport, self.store)
        self.zip_path = self._make_archive()

    def tearDown(self):
        self._tmp.cleanup()

    def _make_archive(self) -> Path:
        """Точный состав настоящего Хорш.zip: PDF+PPTX+DOCX + 4 MP3 + 1 M4A."""
        staging = self.tmp / "staging"
        staging.mkdir()
        # 3 документа — как в реальном архиве
        (staging / "brief.pdf").write_bytes(
            b"%PDF-1.4\n%synthetic brief%\n%%EOF\n")
        (staging / "deck.pptx").write_bytes(_minimal_ooxml(
            "ppt/presentation.xml"))
        (staging / "spec.docx").write_bytes(_minimal_ooxml(
            "word/document.xml"))
        # 5 аудио — как в реальном архиве: 4 MP3 + 1 M4A («длинная» — 7.2 с)
        for name, duration in (("voice-01.mp3", 1.2),
                               ("voice-02.mp3", 1.5),
                               ("voice-03.mp3", 1.0),
                               ("voice-04.mp3", 1.3),
                               ("meeting-long.m4a", 7.2)):
            make_sine(staging / name, duration)
        zip_path = self.tmp / "khorsh-synthetic.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for path in sorted(staging.iterdir()):
                zf.write(path, path.name)
        return zip_path

    def test_full_acceptance_flow(self):
        documents: list[dict] = []

        def document_handler(path: Path, meta: dict) -> None:
            documents.append(meta)

        dest = self.tmp / "imports" / "khorsh"
        manifest = import_zip(self.zip_path, self.engine,
                              document_handler=document_handler,
                              dest_dir=dest)
        # 3 документа (PDF + PPTX + DOCX — PPTX больше НЕ отклоняется)
        self.assertEqual(manifest["totals"]["documents"], 3)
        self.assertEqual(len(documents), 3)
        self.assertEqual({d["name"] for d in documents},
                         {"brief.pdf", "deck.pptx", "spec.docx"})
        # 5 аудио стали заданиями
        self.assertEqual(manifest["totals"]["audio_jobs"], 5)
        self.assertEqual(manifest["totals"]["rejected"], 0)
        jobs = self.store.list_jobs()
        self.assertEqual(len(jobs), 5)

        # обработка всей очереди настоящим конвейером (ffmpeg) + fake прокси
        stats = self.engine.run_pending()
        self.assertEqual(stats["processed"], 5)

        schema = json.loads(
            (Path(__file__).parent.parent / "docs" / "schema" /
             "v1-result.schema.json").read_text(encoding="utf-8"))
        total_segments = 0
        for job in self.store.list_jobs():
            result = self._result(job["id"])
            self.assertEqual(result["status"], "completed",
                             f"job {job['id']} not completed")
            errors = validate(result, schema)
            self.assertEqual(errors, [])
            self.assertIsNotNone(result["transcript"])
            self.assertIsNotNone(result["structured_draft"])
            total_segments += len(result["segments"])
        # длинная запись дала несколько сегментов (сегмент 3 с)
        self.assertGreater(total_segments, 5)
        # исходные форматы подтверждены ffprobe: 4 MP3 + 1 M4A
        mimes = {self.store.get_job(j["id"])["source_mime"]
                 for j in self.store.list_jobs()}
        self.assertEqual(mimes, {"audio/mpeg", "audio/mp4"})
        self.assertEqual(
            sum(1 for j in self.store.list_jobs()
                if self.store.get_job(j["id"])["source_mime"] == "audio/mpeg"),
            4)
        self.assertEqual(
            sum(1 for j in self.store.list_jobs()
                if self.store.get_job(j["id"])["source_mime"] == "audio/mp4"),
            1)
        # конвертированные сегменты не остались на диске (политика очистки)
        seg_dirs = list((Path(self.engine.config.jobs_dir)).glob("*/segments"))
        for seg_dir in seg_dirs:
            leftovers = [p for p in seg_dir.iterdir() if p.suffix == ".ogg"]
            self.assertEqual(leftovers, [])

    def test_acceptance_markdown_export(self):
        from secretary.result import render_markdown
        documents = []

        import_zip(self.zip_path, self.engine,
                   document_handler=lambda p, m: documents.append(m),
                   dest_dir=self.tmp / "imports2" / "khorsh")
        self.engine.run_pending()
        job = self.store.list_jobs()[0]
        result = self._result(job["id"])
        markdown = render_markdown(result)
        self.assertIn("Полная расшифровка", markdown)
        self.assertIn("[SEG 0]", markdown)

    def _result(self, job_id: str) -> dict:
        from secretary.result import build_result
        return build_result(self.store.get_job(job_id),
                            self.store.list_segments(job_id))


def _minimal_ooxml(inner_path: str) -> bytes:
    """Минимальный валидный OOXML-контейнер (zip) на вымышленных данных."""
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.'
                    'openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '</Types>')
        zf.writestr(inner_path,
                    '<?xml version="1.0"?><document>synthetic</document>')
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
