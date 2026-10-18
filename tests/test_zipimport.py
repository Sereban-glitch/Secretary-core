"""Тесты слоя ZIP-импорта: маршрутизация, безопасность, лимиты."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from secretary.config import SecretaryConfig
from secretary.engine import TranscriptionEngine
from secretary.errors import ZipImportError
from secretary.store import Store
from secretary.zipimport import import_zip
from tests.fakes import FakeProber, FakeTransport, make_fake_extractor
from tests.test_engine import make_config


def make_engine(tmp: str):
    config = make_config(tmp)
    transport = FakeTransport()
    store = Store(config.db_path)
    return TranscriptionEngine(config, transport, store,
                               prober=FakeProber.probe,
                               extractor=make_fake_extractor()), transport


def write_zip(path: Path, entries: dict[str, bytes], *,
              symlink_names: set[str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            if symlink_names and name in symlink_names:
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o120777 << 16)
                zf.writestr(info, b"/etc/passwd")
            else:
                zf.writestr(name, data)
    return path


OGG_HEAD = b"OggS\x00\x02"


class ZipImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.engine, self.transport = make_engine(str(self.tmp))
        self.handled: list[dict] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _handler(self, path: Path, meta: dict) -> None:
        self.handled.append(meta)

    def _import(self, zip_path: Path, **kwargs) -> dict:
        dest = self.tmp / "imports" / zip_path.stem
        return import_zip(zip_path, self.engine, document_handler=self._handler,
                          dest_dir=dest, **kwargs)

    def test_audio_documents_and_manifest(self):
        zip_path = write_zip(self.tmp / "batch.zip", {
            "report.pdf": b"%PDF-1.4 synthetic document\n",
            "notes.txt": "текстовая заметка".encode("utf-8"),
            "spec.md": "# Спецификация".encode("utf-8"),
            "voice-30.ogg": OGG_HEAD + b"\x00" * 40,
            "meeting-65.wav": b"RIFF____WAVE" + b"\x00" * 40,
        })
        manifest = self._import(zip_path)
        self.assertEqual(manifest["totals"]["audio_jobs"], 2)
        self.assertEqual(manifest["totals"]["documents"], 3)
        self.assertEqual(len(self.handled), 3)
        names = {h["name"] for h in self.handled}
        self.assertEqual(names, {"report.pdf", "notes.txt", "spec.md"})
        self.assertTrue(all("sha256" in h for h in self.handled))
        # manifest.json записан
        manifest_path = Path(manifest["manifest_path"])
        self.assertTrue(manifest_path.is_file())
        # аудио стали заданиями с валидными сигнатурами контейнера
        jobs = self.engine.store.list_jobs()
        self.assertEqual(len(jobs), 2)

    def test_traversal_rejected(self):
        zip_path = write_zip(self.tmp / "evil.zip", {
            "ok-30.ogg": OGG_HEAD + b"\x00" * 40,
            "../evil.txt": b"payload",
        })
        manifest = self._import(zip_path)
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("traversal", rejected[0]["reason"])
        self.assertFalse((self.tmp / "imports" / "evil.txt").exists())

    def test_symlink_rejected(self):
        zip_path = write_zip(self.tmp / "link.zip", {
            "ok-30.ogg": OGG_HEAD + b"\x00" * 40,
            "link.bin": b"x",
        }, symlink_names={"link.bin"})
        manifest = self._import(zip_path)
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertTrue(any("symlink" in e["reason"] for e in rejected))

    def test_nested_archive_rejected(self):
        inner = self.tmp / "inner.zip"
        write_zip(inner, {"x.txt": b"inner"})
        zip_path = write_zip(self.tmp / "outer.zip", {
            "ok-30.ogg": OGG_HEAD + b"\x00" * 40,
            "nested.zip": inner.read_bytes(),
        })
        manifest = self._import(zip_path)
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertTrue(any("nested archive" in e["reason"] for e in rejected))
        self.assertFalse((self.tmp / "imports" / "outer" / "nested.zip").exists())

    def test_bomb_ratio_rejected(self):
        zip_path = write_zip(self.tmp / "bomb.zip", {
            "ok-30.ogg": OGG_HEAD + b"\x00" * 40,
            "zeros.bin": b"\x00" * (2 * 1024 * 1024),  # высокий коэффициент
        })
        manifest = self._import(zip_path)
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertTrue(any("ratio" in e["reason"] for e in rejected))

    def test_unsupported_type_rejected(self):
        zip_path = write_zip(self.tmp / "mixed.zip", {
            "ok-30.ogg": OGG_HEAD + b"\x00" * 40,
            "program.exe": b"MZ\x90\x00",
        })
        manifest = self._import(zip_path)
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertTrue(any("unsupported type" in e["reason"] for e in rejected))

    def test_audio_content_with_wrong_extension(self):
        zip_path = write_zip(self.tmp / "mismatch.zip", {
            "voice-30.txt": OGG_HEAD + b"\x00" * 40,
        })
        manifest = self._import(zip_path)
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertTrue(any("audio content" in e["reason"] for e in rejected))

    def test_too_many_entries(self):
        entries = {f"f{i}.txt": b"x" for i in range(5)}
        zip_path = write_zip(self.tmp / "many.zip", entries)
        with self.assertRaises(ZipImportError):
            self._import(zip_path, limits={"max_entries": 2})

    def test_total_size_limit(self):
        zip_path = write_zip(self.tmp / "big.zip", {
            "a-30.ogg": OGG_HEAD + b"\x00" * 40,
            "b-30.ogg": OGG_HEAD + b"\x00" * 40,
        })
        with self.assertRaises(ZipImportError):
            self._import(zip_path, limits={"max_total_bytes": 50})

    def test_member_limit(self):
        zip_path = write_zip(self.tmp / "member.zip", {
            "a-30.ogg": OGG_HEAD + b"\x00" * 400,
        })
        manifest = self._import(zip_path, limits={"max_member_bytes": 50})
        rejected = [e for e in manifest["entries"] if e["kind"] == "rejected"]
        self.assertTrue(any("member exceeds" in e["reason"] for e in rejected))

    def test_handler_error_recorded(self):
        zip_path = write_zip(self.tmp / "cb.zip", {
            "notes.txt": b"doc",
            "a-30.ogg": OGG_HEAD + b"\x00" * 40,
        })

        def failing_handler(path: Path, meta: dict) -> None:
            raise RuntimeError("client handler failure")

        dest = self.tmp / "imports" / "cb"
        manifest = import_zip(zip_path, self.engine,
                              document_handler=failing_handler,
                              dest_dir=dest)
        entry = next(e for e in manifest["entries"] if e["kind"] == "document")
        self.assertIn("error: client handler failure", entry["handler"])

    def test_duplicate_audio_deduplicated(self):
        zip_path = write_zip(self.tmp / "dup.zip", {
            "a-30.ogg": OGG_HEAD + b"\x00" * 40,
        })
        manifest = self._import(zip_path)
        self.assertEqual(manifest["totals"]["audio_jobs"], 1)
        manifest2 = self._import(zip_path)  # второй раз — тот же job_id
        self.assertEqual(manifest2["totals"]["audio_jobs"], 1)
        self.assertEqual(len(self.engine.store.list_jobs()), 1)

    def test_bad_zip_rejected(self):
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"PK\x03\x04 corrupted")
        with self.assertRaises(ZipImportError):
            self._import(bad)

    # --------- pass2: allowlist документов синхронизирован с AgroBot ---------

    def test_document_allowlist_matches_agrobot_admin(self):
        """Множество расширений = EXT_BY_MIME админки AgroBot (+jpeg)."""
        from secretary.zipimport import DOCUMENT_EXTENSIONS
        # kb-docs/storage.ts: pdf, docx, pptx, xlsx, doc, ppt, xls, txt, md,
        # csv, json, jpg, png, webp (+ jpeg как синоним image/jpeg)
        self.assertEqual(
            DOCUMENT_EXTENSIONS,
            {"pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls",
             "txt", "md", "csv", "json", "jpg", "jpeg", "png", "webp"})

    def test_pptx_and_office_formats_accepted(self):
        """Состав настоящего Хорш.zip: PDF + PPTX + DOCX — все документы."""
        import io

        def minimal_ooxml(inner_path: str) -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as zf:
                zf.writestr("[Content_Types].xml",
                            '<?xml version="1.0"?><Types/>')
                zf.writestr(inner_path, '<?xml version="1.0"?>synthetic')
            return buffer.getvalue()

        zip_path = write_zip(self.tmp / "docs.zip", {
            "brief.pdf": b"%PDF-1.4\n%synthetic%\n%%EOF\n",
            "deck.pptx": minimal_ooxml("ppt/presentation.xml"),
            "spec.docx": minimal_ooxml("word/document.xml"),
            "table.xlsx": minimal_ooxml("xl/workbook.xml"),
            "photo.jpg": b"\xff\xd8\xff\xe0 synthetic jpeg",
            "photo2.jpeg": b"\xff\xd8\xff\xe1 synthetic jpeg",
            "picture.png": b"\x89PNG\r\n\x1a\n synthetic png",
            "image.webp": b"RIFF\x00\x00\x00\x00WEBP synthetic",
            "legacy.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 synthetic",
            "legacy.ppt": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 synthetic",
            "legacy.xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 synthetic",
            "data.json": b'{"synthetic": true}',
            "sheet.csv": b"a,b\n1,2\n",
            "note.txt": "текст".encode("utf-8"),
            "note.md": "# md".encode("utf-8"),
        })
        manifest = self._import(zip_path)
        self.assertEqual(manifest["totals"]["documents"], 15)
        self.assertEqual(manifest["totals"]["rejected"], 0)
        self.assertEqual(len(self.handled), 15)
        # манифест несёт предполагаемый MIME (как в админке) + sha256
        pptx_entry = next(e for e in manifest["entries"]
                          if e.get("extracted") == "deck.pptx"
                          or e["name"] == "deck.pptx")
        self.assertIn("presentationml.presentation",
                      pptx_entry["document_mime"])
        self.assertTrue(all("sha256" in h and "mime" in h
                            for h in self.handled))

    def test_document_mime_mismatch_marked_not_rejected(self):
        """Расширение не совпало с контейнером: помечено '?', не брак."""
        zip_path = write_zip(self.tmp / "weird.zip", {
            "fake.pdf": b"NOT-A-PDF-AT-ALL",
        })
        manifest = self._import(zip_path)
        self.assertEqual(manifest["totals"]["documents"], 1)
        entry = manifest["entries"][0]
        self.assertTrue(entry["document_mime"].endswith("?"))


if __name__ == "__main__":
    unittest.main()
