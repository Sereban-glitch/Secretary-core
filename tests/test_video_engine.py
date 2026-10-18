from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from secretary.config import SecretaryConfig
from secretary.result import build_result, render_markdown
from secretary.store import Store
from secretary.video import VideoInfo
from secretary.video_engine import VideoEngine
from tests.minijsonschema import validate


class _Transport:
    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        value = {
            "summary": "Показана работа админки.",
            "speech": "Открываем запись.",
            "visible_text": ["База знаний"],
            "events": [{"time": 1.0, "description": "Нажата кнопка"}],
            "facts": [{"text": "Есть кнопка удаления", "source": "both"}],
            "uncertain": ["Название мелкое"],
        }
        return {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


class VideoEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = SecretaryConfig(
            model="audio-model", video_model="video-model",
            db_path=str(root / "jobs.sqlite3"), jobs_dir=str(root / "jobs"),
            video_segment_seconds=10, video_min_segment_seconds=2,
            video_max_segment_bytes=1024 * 1024,
            disk_budget_bytes=1)
        self.store = Store(self.config.db_path)
        self.transport = _Transport()

        def prober(_path):
            return VideoInfo(12.0, 1280, 720, "mp4", True)

        def extractor(_src, start, end, dest, **_kwargs):
            payload = f"segment:{start}-{end}".encode()
            dest.write_bytes(payload)
            return len(payload)

        self.engine = VideoEngine(
            self.config, self.transport, self.store,
            prober=prober, extractor=extractor)
        self.source = root / "lesson.mp4"
        self.source.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_complete_video_job_and_delete_source_copy(self):
        source_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        created = self.engine.create_job(self.source, owner_id="natasha",
                                         source_url=source_url)
        self.assertEqual(created["media_kind"], "video")
        self.assertEqual(self.engine.run_job(created["job_id"]), "completed")
        job = self.store.get_job(created["job_id"])
        self.assertEqual(job["media_kind"], "video")
        self.assertFalse((Path(self.config.jobs_dir) / created["job_id"] /
                          job["source_path"]).exists())
        result = build_result(job, self.store.list_segments(created["job_id"]))
        self.assertEqual(result["media_kind"], "video")
        self.assertEqual(result["provenance"]["source_type"], "youtube")
        self.assertEqual(result["provenance"]["source_url"], source_url)
        schema = json.loads((Path(__file__).resolve().parent.parent /
                             "docs/schema/v1-result.schema.json").read_text())
        self.assertEqual(validate(result, schema), [])
        self.assertIn("Нажата кнопка", result["transcript"])
        self.assertIn("# Видеоанализ", render_markdown(result))
        self.assertEqual(len(self.transport.requests), 2)

    def test_deduplicates_same_owner_source_and_params(self):
        first = self.engine.create_job(self.source, owner_id="natasha")
        second = self.engine.create_job(self.source, owner_id="natasha")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["job_id"], second["job_id"])


if __name__ == "__main__":
    unittest.main()
