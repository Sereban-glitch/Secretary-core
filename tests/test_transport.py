"""Тесты HttpTransport против локального loopback-мока (реальный urllib)."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from secretary.errors import (PermanentTransportError,
                              TransientTransportError)
from secretary.transport import HttpTransport


class _MockProxy:
    """Скриптованный прокси: очередь ответов (status, headers, body)."""

    def __init__(self):
        self.responses = []
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer.lock:
                    outer.requests.append(
                        {"path": self.path,
                         "content_type": self.headers.get("Content-Type"),
                         "x_api_key": self.headers.get("x-api-key"),
                         "authorization": self.headers.get("Authorization"),
                         "body": body})
                    item = (outer.responses.pop(0)
                            if outer.responses else (200, {}, b'{"stop_reason":"end_turn","content":[]}'))
                status, headers, payload = item
                if status == "sleep":
                    import time
                    time.sleep(headers.get("seconds", 1.0))
                    status, headers, payload = 200, {}, b"{}"
                if status == "echo-key":  # тело ответа содержит ключ (санитизация)
                    key = self.headers.get("x-api-key", "")
                    status, headers, payload = 500, {}, f"boom {key}".encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:  # клиент уже закрыл (тест таймаута)
                    pass

            def log_message(self, fmt, *args):  # тихо
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _ok_body() -> bytes:
    return json.dumps({"stop_reason": "end_turn",
                       "content": [{"type": "text", "text": "ok"}]}).encode()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.proxy = _MockProxy()

    def tearDown(self):
        self.proxy.stop()

    def _transport(self, **kwargs):
        kwargs.setdefault("timeout", 5.0)
        return HttpTransport(self.proxy.url, "KEY-123", **kwargs)

    def test_success_json(self):
        self.proxy.responses = [(200, {}, _ok_body())]
        response = self._transport().send({"model": "m"})
        self.assertEqual(response["stop_reason"], "end_turn")
        sent = self.proxy.requests[0]
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertEqual(sent["content_type"], "application/json")
        self.assertEqual(sent["x_api_key"], "KEY-123")
        self.assertIsNone(sent["authorization"])

    def test_authorization_bearer_variant(self):
        self.proxy.responses = [(200, {}, _ok_body())]
        transport = self._transport(auth_header="authorization")
        transport.send({"model": "m"})
        self.assertEqual(self.proxy.requests[0]["authorization"], "Bearer KEY-123")
        self.assertIsNone(self.proxy.requests[0]["x_api_key"])

    def test_429_with_retry_after(self):
        self.proxy.responses = [(429, {"Retry-After": "7"}, b"slow down")]
        with self.assertRaises(TransientTransportError) as ctx:
            self._transport().send({"model": "m"})
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_5xx_transient(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.proxy.responses = [(status, {}, b"")]
                with self.assertRaises(TransientTransportError):
                    self._transport().send({"model": "m"})

    def test_4xx_permanent(self):
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.proxy.responses = [(status, {}, b"nope")]
                with self.assertRaises(PermanentTransportError):
                    self._transport().send({"model": "m"})

    def test_invalid_json_200_is_transient(self):
        self.proxy.responses = [(200, {}, b'{"trunc')]
        with self.assertRaises(TransientTransportError):
            self._transport().send({"model": "m"})

    def test_non_object_200_is_permanent(self):
        self.proxy.responses = [(200, {}, b'[1,2,3]')]
        with self.assertRaises(PermanentTransportError):
            self._transport().send({"model": "m"})

    def test_redirect_not_followed(self):
        self.proxy.responses = [(302, {"Location": "http://evil.invalid/"}, b"")]
        with self.assertRaises(PermanentTransportError):
            self._transport().send({"model": "m"})
        # второй запрос не отправлялся — редирект не отслеживался
        self.assertEqual(len(self.proxy.requests), 1)

    def test_timeout_is_transient(self):
        self.proxy.responses = [("sleep", {"seconds": 1.5}, b"")]
        transport = self._transport(timeout=0.2)
        with self.assertRaises(TransientTransportError):
            transport.send({"model": "m"})

    def test_key_never_leaks_into_error(self):
        self.proxy.responses = [("echo-key", {}, b"")]
        with self.assertRaises(TransientTransportError) as ctx:
            self._transport().send({"model": "m"})
        self.assertNotIn("KEY-123", ctx.exception.detail)
        self.assertIn("***", ctx.exception.detail)

    def test_https_enforced_remote(self):
        # http для удалённого хоста отклоняется ДО соединения с сетью
        transport = HttpTransport("http://proxy.example.com", "k",
                                  allow_loopback_http=True)
        with self.assertRaises(PermanentTransportError):
            transport.send({"model": "m"})
        self.assertEqual(self.proxy.requests, [])  # никуда не ходили

    def test_loopback_http_allowed(self):
        # сам мок — loopback http; успех уже проверен test_success_json
        transport = HttpTransport(self.proxy.url, "k",
                                  allow_loopback_http=False)
        with self.assertRaises(PermanentTransportError):
            transport.send({"model": "m"})

    def test_userinfo_rejected(self):
        transport = HttpTransport("http://user:pass@127.0.0.1:1", "k")
        with self.assertRaises(PermanentTransportError):
            transport.send({"model": "m"})

    def test_repr_has_no_key(self):
        transport = self._transport()
        self.assertNotIn("KEY-123", repr(transport))


if __name__ == "__main__":
    unittest.main()
