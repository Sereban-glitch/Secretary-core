"""Офлайн-тесты безопасной загрузки по ссылке: сеть не используется."""
from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path

from secretary.errors import UrlImportError
from secretary.urlimport import (download_url, normalize_google_drive_url,
                                 _resolve_public)


class FakeResponse:
    def __init__(self, body=b"audio", *, status=200, headers=None):
        self.status = status
        self._stream = io.BytesIO(body)
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, size=-1):
        return self._stream.read(size)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def public_resolver(host, port, type=None):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class UrlImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dest = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _download(self, responses, **kwargs):
        queue = list(responses)

        def opener(url, **unused):
            response = queue.pop(0)
            return FakeConnection(), response

        return download_url(
            "https://files.example.org/source.ogg", self.dest,
            max_bytes=kwargs.pop("max_bytes", 1024),
            min_free_after_bytes=0, opener=opener,
            resolver=public_resolver, **kwargs)

    def test_download_streams_and_hashes(self):
        body = b"OggS synthetic audio"
        result = self._download([FakeResponse(body, headers={
            "Content-Length": str(len(body)),
            "Content-Type": "audio/ogg",
            "Content-Disposition": "attachment; filename*=UTF-8''voice%20one.ogg",
        })])
        self.assertEqual(Path(result.path).read_bytes(), body)
        self.assertEqual(result.filename, "voice one.ogg")
        self.assertEqual(result.sha256, hashlib.sha256(body).hexdigest())
        self.assertFalse(list(self.dest.glob("*.part")))

    def test_drive_share_url_is_normalized(self):
        normalized = normalize_google_drive_url(
            "https://drive.google.com/file/d/ABC_123-xy/view?usp=sharing")
        self.assertEqual(
            normalized,
            "https://drive.usercontent.google.com/download?export=download&confirm=t&id=ABC_123-xy")

    def test_http_userinfo_and_private_dns_are_rejected(self):
        for url in ("http://example.org/a.ogg",
                    "https://user:secret@example.org/a.ogg"):
            with self.subTest(url=url), self.assertRaises(UrlImportError):
                download_url(url, self.dest, max_bytes=10,
                             min_free_after_bytes=0)

        def private_resolver(host, port, type=None):
            return [(2, 1, 6, "", ("127.0.0.1", port))]

        with self.assertRaises(UrlImportError):
            _resolve_public("example.org", 443, private_resolver)

    def test_redirect_is_revalidated_and_followed(self):
        seen = []
        responses = [
            FakeResponse(b"", status=302, headers={"Location": "/real.ogg"}),
            FakeResponse(b"ok", headers={"Content-Type": "audio/ogg"}),
        ]

        def opener(url, **unused):
            seen.append(url)
            return FakeConnection(), responses.pop(0)

        result = download_url(
            "https://files.example.org/start", self.dest, max_bytes=10,
            min_free_after_bytes=0, opener=opener, resolver=public_resolver)
        self.assertEqual(seen[-1], "https://files.example.org/real.ogg")
        self.assertEqual(Path(result.path).read_bytes(), b"ok")

    def test_header_and_stream_size_limits_remove_partial(self):
        with self.assertRaisesRegex(UrlImportError, "превышает"):
            self._download([FakeResponse(b"x", headers={"Content-Length": "100"})],
                           max_bytes=10)
        self.assertFalse(list(self.dest.glob("*.part")))

        with self.assertRaisesRegex(UrlImportError, "превысил"):
            self._download([FakeResponse(b"12345678901")], max_bytes=10)
        self.assertFalse(list(self.dest.glob("*.part")))

    def test_html_login_page_and_empty_file_are_rejected(self):
        with self.assertRaisesRegex(UrlImportError, "веб-страница"):
            self._download([FakeResponse(b"<html>login</html>", headers={
                "Content-Type": "text/html; charset=utf-8"})])
        with self.assertRaisesRegex(UrlImportError, "пустой"):
            self._download([FakeResponse(b"")])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_unsafe_filename_is_sanitized(self):
        result = self._download([FakeResponse(b"x", headers={
            "Content-Disposition": 'attachment; filename="../../voice?.ogg"',
            "Content-Type": "audio/ogg",
        })])
        self.assertEqual(result.filename, "voice_.ogg")
        self.assertEqual(Path(result.path).parent, self.dest)

    def test_repeated_url_never_overwrites_existing_file(self):
        first = self._download([FakeResponse(b"one")])
        second = self._download([FakeResponse(b"two")])
        third = self._download([FakeResponse(b"three")])
        self.assertEqual(Path(first.path).read_bytes(), b"one")
        self.assertEqual(Path(second.path).read_bytes(), b"two")
        self.assertEqual(Path(third.path).read_bytes(), b"three")
        self.assertEqual(len({first.path, second.path, third.path}), 3)


if __name__ == "__main__":
    unittest.main()
