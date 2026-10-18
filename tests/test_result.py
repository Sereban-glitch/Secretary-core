"""Тесты результата v1: сборка, Markdown, схема, примеры на вымышленных данных."""
import json
import tempfile
import unittest
from pathlib import Path

from secretary.result import build_result, render_markdown
from tests.minijsonschema import validate

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "schema" / \
    "v1-result.schema.json"
EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "docs" / "examples" / \
    "v1-result.example.json"


def sample_job() -> dict:
    return {
        "id": "a" * 32, "created_at": 1750000000.0, "updated_at": 1750000600.0,
        "status": "completed", "source_filename": "demo.ogg",
        "source_sha256": "b" * 64, "source_mime": "audio/ogg",
        "source_duration": 65.0, "source_size": 12345,
        "transcript": "[SEG 0] 00:00:00–00:00:30\nТекст.",
        "structured_json": {"summary": "Сводка.", "topics": [],
                            "statements": [], "questionable_places": [],
                            "speakers_note": ""},
        "warnings_json": ["demo warning"],
        "profile_json": {"model": "m", "transcribe_prompt_version": "v1",
                         "structure_prompt_version": "v1",
                         "structure_merge_prompt_version": "v1",
                         "segment_seconds": 30.0, "overlap_seconds": 4.0,
                         "audio_pipeline": "ffmpeg", "transport": "Fake",
                         "language": None, "vocabulary_terms": 0},
        "error": None,
    }


def sample_segments() -> list[dict]:
    return [
        {"idx": 0, "start": 0.0, "end": 30.0, "status": "done",
         "text": "Текст.", "warnings_json": []},
        {"idx": 1, "start": 26.0, "end": 65.0, "status": "failed",
         "text": None, "warnings_json": []},
    ]


class BuildResultTests(unittest.TestCase):
    def test_shape(self):
        result = build_result(sample_job(), sample_segments())
        self.assertEqual(result["version"], "1")
        self.assertEqual(result["source"]["duration"], 65.0)
        self.assertEqual(result["segments"][0]["id"], 0)
        self.assertEqual(result["segments"][1]["status"], "failed")
        self.assertEqual(result["provenance"]["model"], "m")
        self.assertIn("created_at", result)

    def test_validates_against_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        result = build_result(sample_job(), sample_segments())
        errors = validate(result, schema)
        self.assertEqual(errors, [])

    def test_markdown_render(self):
        result = build_result(sample_job(), sample_segments())
        markdown = render_markdown(result)
        self.assertIn("# Расшифровка: demo.ogg", markdown)
        self.assertIn("## Структурированный черновик", markdown)
        self.assertIn("## Полная расшифровка", markdown)
        self.assertIn("demo warning", markdown)
        self.assertIn("job_id", markdown)


class ExampleFileTests(unittest.TestCase):
    def test_example_validates_against_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        errors = validate(example, schema)
        self.assertEqual(errors, [])

    def test_example_markdown_renders(self):
        example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        markdown = render_markdown(example)
        self.assertIn("Структурированный черновик", markdown)
        self.assertIn("Полная расшифровка", markdown)
        self.assertIn("210 литров", markdown)  # фиктивные данные сохранены

    def test_example_is_fictional(self):
        # имена файлов/модели демо-примера не претендуют на реальные
        example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        self.assertIn("demo", example["source"]["filename"])
        self.assertIn("demo", example["provenance"]["model"])


class MarkdownFileTest(unittest.TestCase):
    def test_write_markdown_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = build_result(sample_job(), sample_segments())
            path = Path(tmp) / "out.md"
            path.write_text(render_markdown(result), encoding="utf-8")
            self.assertTrue(path.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
