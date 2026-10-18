"""E2E-тесты HTTP API: сервер + воркер, всё офлайн (fake транспорт/аудио)."""
import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from secretary.config import SecretaryConfig
from secretary.engine import TranscriptionEngine
from secretary.errors import ConfigError, UrlImportError, YouTubeImportError
from secretary.server import make_server
from secretary.store import Store
from secretary.urlimport import DownloadResult
from secretary.youtube import YouTubeDownload
from tests.fakes import FakeProber, FakeTransport, make_fake_extractor
from tests.test_engine import make_config

WAV_HEAD = b"RIFF\x24\x08\x00\x00WAVE"
BODY_61 = WAV_HEAD + b"\x00" * 56 + b"DUR:61.0"  # сигнатура + маркер длительности
# тело больше одного стримингового чанка (1 МиB) — маркер в первых 512 байтах
BODY_BIG = WAV_HEAD + b"DUR:61.0" + b"\x00" * (2 * 1024 * 1024 + 12345)


def make_engine(tmp: str, **cfg) -> tuple[TranscriptionEngine, FakeTransport]:
    config = make_config(tmp, **cfg)
    transport = FakeTransport()
    store = Store(config.db_path)
    engine = TranscriptionEngine(config, transport, store,
                                 prober=FakeProber.probe,
                                 extractor=make_fake_extractor())
    return engine, transport


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.engine, self.transport = make_engine(self.tmp)
        self.server, self.worker = make_server(self.engine, host="127.0.0.1",
                                               port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self.thread.start()

    def tearDown(self):
        self.worker.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str, token: str | None = None,
             headers: dict | None = None):
        request = urllib.request.Request(self._url(path))
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _get_raw(self, path: str):
        with urllib.request.urlopen(self._url(path), timeout=5) as response:
            return response.status, response.read().decode("utf-8"), \
                response.headers.get("Content-Type")

    def _post(self, path: str, body: bytes, headers: dict | None = None,
              token: str | None = None):
        request = urllib.request.Request(self._url(path), data=body,
                                         method="POST")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _create(self, name="audio-61.wav", token=None) -> dict:
        status, payload = self._post(
            "/v1/jobs", BODY_61,
            {"X-Secretary-Filename": name}, token=token)
        self.assertEqual(status, 201)
        return payload

    def _wait_terminal(self, job_id: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, payload = self._get(f"/v1/jobs/{job_id}")
            if payload["status"] in ("completed", "partial", "failed",
                                     "cancelled"):
                return payload
            time.sleep(0.05)
        raise AssertionError("job did not finish in time")

    # ------------------------------------------------------------------
    def test_healthz(self):
        status, payload = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_video_upload_uses_separate_video_pipeline(self):
        called = {}

        def create_video_job(path, **kwargs):
            called["bytes"] = Path(path).read_bytes()
            called["kwargs"] = kwargs
            Path(path).unlink(missing_ok=True)
            return {"job_id": "video123", "created": True,
                    "status": "queued", "media_kind": "video"}

        self.engine.create_video_job = create_video_job  # type: ignore[attr-defined]
        body = b"\x00\x00\x00\x18ftypisom" + b"video"
        status, payload = self._post(
            "/v1/video-jobs", body,
            {"X-Secretary-Filename": "lesson.mp4",
             "X-Secretary-Language": "uk"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["media_kind"], "video")
        self.assertEqual(called["bytes"], body)
        self.assertEqual(called["kwargs"]["filename"], "lesson.mp4")
        self.assertTrue(called["kwargs"]["move_source"])

    def test_full_flow_create_process_result(self):
        self.worker.start()
        created = self._create()
        job_id = created["job_id"]
        final = self._wait_terminal(job_id)
        self.assertEqual(final["status"], "completed")
        status, result = self._get(f"/v1/jobs/{job_id}/result")
        self.assertEqual(status, 200)
        self.assertEqual(result["version"], "1")
        self.assertEqual(result["source"]["mime"], "audio/wav")
        self.assertIsNotNone(result["transcript"])
        self.assertIsNotNone(result["structured_draft"])
        self.assertEqual(len(result["segments"]), 2)
        status, markdown, ctype = self._get_raw(f"/v1/jobs/{job_id}/markdown")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", ctype)
        self.assertIn("Полная расшифровка", markdown)
        # список заданий
        status, listing = self._get("/v1/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(listing["jobs"][0]["job_id"], job_id)

    def test_result_409_while_in_progress(self):
        created = self._create()  # воркер НЕ запущен
        try:
            self._get(f"/v1/jobs/{created['job_id']}/result")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"]["type"], "not_finished")

    def test_create_requires_filename(self):
        try:
            self._post("/v1/jobs", WAV_HEAD + b"\x00" * 64)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_create_accepts_utf8_filename_via_base64_header(self):
        encoded = base64.b64encode("Відгук Берту.wav".encode("utf-8")).decode("ascii")
        status, payload = self._post(
            "/v1/jobs", BODY_61,
            {"X-Secretary-Filename": "audio.wav",
             "X-Secretary-Filename-B64": encoded})
        self.assertEqual(status, 201)
        job = self.engine.store.get_job(payload["job_id"])
        self.assertEqual(job["source_filename"], "Відгук Берту.wav")

    def test_create_rejects_invalid_base64_filename(self):
        try:
            self._post(
                "/v1/jobs", BODY_61,
                {"X-Secretary-Filename": "audio.wav",
                 "X-Secretary-Filename-B64": "%%%not-base64%%%"})
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_create_rejects_unsafe_filename(self):
        for name in ("../evil.wav", "a/b.wav", ".hidden.wav"):
            with self.subTest(name=name):
                try:
                    self._post("/v1/jobs", WAV_HEAD + b"\x00" * 8,
                               {"X-Secretary-Filename": name})
                    self.fail("expected HTTPError")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 400)

    def test_create_rejects_bad_audio(self):
        try:
            self._post("/v1/jobs", b"garbagegarbage" * 8,
                       {"X-Secretary-Filename": "audio-61.wav"})
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_unknown_job_404(self):
        for path in ("/v1/jobs/missing", "/v1/jobs/missing/result"):
            with self.subTest(path=path):
                try:
                    self._get(path)
                    self.fail("expected HTTPError")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 404)

    def test_expected_owner_header_hides_other_owner(self):
        status, created = self._post(
            "/v1/jobs", BODY_61,
            {"X-Secretary-Filename": "owner.wav",
             "X-Secretary-Owner": "knowledge-admin:42"})
        self.assertEqual(status, 201)
        job_id = created["job_id"]
        status, _ = self._get(
            f"/v1/jobs/{job_id}",
            headers={"X-Secretary-Expected-Owner": "knowledge-admin:42"})
        self.assertEqual(status, 200)
        try:
            self._get(
                f"/v1/jobs/{job_id}",
                headers={"X-Secretary-Expected-Owner": "knowledge-admin:99"})
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_cancel_flow(self):
        self.worker.start()
        # транспорт, который «зависает» на втором сегменте: отмена между ними
        holder = {}

        def responder(request):
            from tests.fakes import ok_text
            marker = FakeTransport.decode_marker(request)
            if marker is not None:
                if marker[0] >= 26:
                    holder["engine"].cancel(holder["job_id"])
                    return ok_text("Второй сегмент.")
                return ok_text("Первый сегмент.")
            return ok_text(json.dumps(
                {"summary": "s", "topics": [], "statements": [],
                 "questionable_places": [], "speakers_note": ""}))

        holder["engine"] = self.engine
        self.transport._responder = responder
        created = self._create()
        holder["job_id"] = created["job_id"]
        final = self._wait_terminal(created["job_id"])
        self.assertEqual(final["status"], "cancelled")

    def test_retry_failed_endpoint(self):
        self.worker.start()

        def responder(request):
            from tests.fakes import ok_text
            from secretary.errors import PermanentTransportError
            marker = FakeTransport.decode_marker(request)
            if marker is not None and marker[0] >= 26:
                raise PermanentTransportError("model refused")
            if marker is not None:
                return ok_text("Первый сегмент.")
            raise PermanentTransportError("no structuring")

        self.transport._responder = responder
        created = self._create()
        final = self._wait_terminal(created["job_id"])
        self.assertEqual(final["status"], "partial")
        # транспорт «починился», retry-failed возвращает в очередь
        self.transport._responder = FakeTransport()._default_responder
        status, payload = self._post(
            f"/v1/jobs/{created['job_id']}/retry-failed", b"")
        self.assertEqual(status, 200)
        self.assertEqual(payload["requeued"], 1)
        final = self._wait_terminal(created["job_id"])
        self.assertEqual(final["status"], "completed")

    def test_token_auth(self):
        import dataclasses
        self.server.RequestHandlerClass.api_token = "TOKEN"
        try:
            self._get("/healthz")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
        status, _ = self._get("/healthz", token="TOKEN")
        self.assertEqual(status, 200)
        self.assertEqual(
            dataclasses.replace(self.engine.config, api_token="TOKEN").api_token,
            "TOKEN")

    def test_duplicate_create_returns_200(self):
        self.worker.start()
        first = self._create()
        self._wait_terminal(first["job_id"])
        status, second = self._post(
            "/v1/jobs", BODY_61,
            {"X-Secretary-Filename": "audio-61.wav"})
        self.assertEqual(status, 200)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertFalse(second["created"])

    def test_url_job_downloads_then_creates_job(self):
        downloaded = Path(self.tmp) / "from-drive.wav"

        def fake_download(*args, **kwargs):
            downloaded.write_bytes(BODY_61)
            return DownloadResult(
                str(downloaded), downloaded.name, len(BODY_61), "a" * 64,
                "audio/wav", "drive.usercontent.google.com")

        body = json.dumps({
            "url": "https://drive.google.com/file/d/ABC/view",
            "language": "uk",
            "vocabulary": ["BERTHOUD", "Raptor"],
        }).encode("utf-8")
        with mock.patch("secretary.server.download_url",
                        side_effect=fake_download) as patched:
            status, payload = self._post(
                "/v1/url-jobs", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["download"]["source_host"],
                         "drive.usercontent.google.com")
        self.assertFalse(downloaded.exists())  # перенесён в каталог задания
        patched.assert_called_once()

    def test_repeated_url_audio_is_deduplicated_for_same_owner(self):
        downloaded = Path(self.tmp) / "repeat.wav"

        def fake_download(*args, **kwargs):
            downloaded.write_bytes(BODY_61)
            return DownloadResult(
                str(downloaded), downloaded.name, len(BODY_61), "b" * 64,
                "audio/wav", "files.example.org")

        body = json.dumps({
            "url": "https://files.example.org/repeat.wav",
            "owner": "natalia",
            "language": "uk",
        }).encode("utf-8")
        with mock.patch("secretary.server.download_url",
                        side_effect=fake_download):
            first_status, first = self._post("/v1/url-jobs", body)
            second_status, second = self._post("/v1/url-jobs", body)
        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 200)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.engine.store.list_jobs()), 1)

    def test_url_job_reports_safe_error_without_creating_job(self):
        body = json.dumps({
            "url": "https://drive.google.com/file/d/PRIVATE/view",
        }).encode("utf-8")
        with mock.patch("secretary.server.download_url",
                        side_effect=UrlImportError("закрытая ссылка")):
            try:
                self._post("/v1/url-jobs", body,
                           {"Content-Type": "application/json"})
                self.fail("expected HTTPError")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                payload = json.loads(exc.read().decode("utf-8"))
                self.assertEqual(payload["error"]["type"], "url_import")
        self.assertEqual(self.engine.store.list_jobs(), [])

    def test_url_job_rejects_invalid_json_shape(self):
        for body in (b"[]", b'{"url":""}',
                     b'{"url":"https://example.org/a.wav","vocabulary":"x"}'):
            with self.subTest(body=body):
                try:
                    self._post("/v1/url-jobs", body,
                               {"Content-Type": "application/json"})
                    self.fail("expected HTTPError")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 400)

    def test_youtube_job_downloads_and_creates_video_job(self):
        downloaded = Path(self.tmp) / "youtube-test.mp4"
        downloaded.write_bytes(b"video")
        item = YouTubeDownload(
            str(downloaded), "dQw4w9WgXcQ", "Навчальне відео", 42.0,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", 5)
        create = mock.Mock(return_value={
            "job_id": "video-job", "created": True, "status": "queued"})
        self.engine.create_video_job = create
        body = json.dumps({
            "url": "https://youtu.be/dQw4w9WgXcQ", "owner": "natasha",
            "language": "uk", "vocabulary": ["BERTHOUD"],
        }).encode()
        with mock.patch("secretary.server.download_youtube", return_value=item):
            status, payload = self._post("/v1/youtube-jobs", body)
        self.assertEqual(status, 201)
        self.assertEqual(payload["youtube"]["video_id"], "dQw4w9WgXcQ")
        create.assert_called_once_with(
            downloaded, filename="Навчальне відео.mp4", owner_id="natasha",
            language="uk", vocabulary=["BERTHOUD"], move_source=True,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_youtube_job_reports_safe_error(self):
        body = json.dumps({"url": "https://youtu.be/dQw4w9WgXcQ"}).encode()
        with mock.patch("secretary.server.download_youtube",
                        side_effect=YouTubeImportError("закрытое видео")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post("/v1/youtube-jobs", body)
        self.assertEqual(caught.exception.code, 400)
        payload = json.loads(caught.exception.read().decode())
        self.assertEqual(payload["error"]["type"], "youtube_import")

    # ------------- pass2: потоковая загрузка и защита интерфейса ------------

    def test_streaming_upload_multichunk_body(self):
        """Тело > 1 чанка пишется на диск потоково и задаётся корректно."""
        self.worker.start()
        status, payload = self._post(
            "/v1/jobs", BODY_BIG, {"X-Secretary-Filename": "big.wav"})
        self.assertEqual(status, 201)
        final = self._wait_terminal(payload["job_id"])
        self.assertEqual(final["status"], "completed")
        # исходник лёг в job-директорию целиком (перенос, не обрезка)
        job = self.engine.store.get_job(payload["job_id"])
        src = Path(self.engine.jobs_dir) / payload["job_id"] / job["source_path"]
        self.assertEqual(src.stat().st_size, len(BODY_BIG))
        # temp-файлы загрузки не остались в jobs_dir
        leftovers = [p for p in Path(self.engine.jobs_dir).iterdir()
                     if p.name.startswith("upload-")]
        self.assertEqual(leftovers, [])

    def test_streaming_upload_rejects_over_limit(self):
        """Превышение лимита по Content-Length — отказ без чтения тела."""
        small = make_engine(self.tmp, max_source_bytes=32)
        self.engine, self.transport = small
        # подменяем engine в классе обработчика уже запущенного сервера
        self.server.RequestHandlerClass.engine = self.engine
        try:
            self._post("/v1/jobs", BODY_61,
                       {"X-Secretary-Filename": "big.wav"})
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn("exceeds limit", exc.read().decode("utf-8"))
        # задания не создано, мусорных файлов в jobs_dir нет
        self.assertEqual(self.engine.store.list_jobs(), [])
        jobs_dir = Path(self.engine.jobs_dir)
        if jobs_dir.exists():
            leftovers = [p for p in jobs_dir.iterdir()
                         if p.name.startswith("upload-")]
            self.assertEqual(leftovers, [])

    def test_chunked_transfer_rejected(self):
        request = urllib.request.Request(self._url("/v1/jobs"),
                                         data=b"5\r\nhello\r\n0\r\n\r\n",
                                         method="POST")
        request.add_header("Transfer-Encoding", "chunked")
        request.add_header("X-Secretary-Filename", "audio-61.wav")
        try:
            with urllib.request.urlopen(request, timeout=5):
                pass
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn("chunked", exc.read().decode("utf-8"))

    def test_non_loopback_bind_requires_token(self):
        from secretary.server import make_server as ms
        with self.assertRaises(ConfigError):
            ms(self.engine, host="0.0.0.0", port=0)
        server, worker = ms(self.engine, host="0.0.0.0", port=0,
                            api_token="TOKEN")
        try:
            self.assertNotEqual(server, None)
        finally:
            server.server_close()
            worker.stop()

    def test_internal_error_sanitized(self):
        """500 не раскрывает деталей исключения клиенту (ревью pass2, P2)."""
        detail = "SECRET-DETAIL-BOOM"
        original = self.engine.progress

        def boom(job_id):
            raise RuntimeError(detail)

        self.engine.progress = boom
        try:
            try:
                self._get("/v1/jobs/whatever")
                self.fail("expected HTTPError")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 500)
                payload = json.loads(exc.read().decode("utf-8"))
                self.assertEqual(payload["error"]["type"], "internal")
                self.assertNotIn(detail, json.dumps(payload))
        finally:
            self.engine.progress = original


if __name__ == "__main__":
    unittest.main()
