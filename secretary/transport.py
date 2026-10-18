"""Транспорт запросов к прокси: интерфейс + HTTP-реализация на stdlib.

Границы безопасности (docs/PROXY.md):
- редиректы не отслеживаются вообще (SSRF-защита);
- HTTPS для удалённых хостов; http — только loopback c явного разрешения;
- ключ передаётся только в заголовке, не в URL, не в лог;
- размер ответа ограничен;
- userinfo в URL запрещён (проверяется в конфигурации и здесь повторно).
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .errors import (PermanentTransportError, TransientTransportError,
                     TransportError)

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Transport:
    """Протокол: send(request: dict) -> response: dict.

    Временные ошибки — TransientTransportError, постоянные —
    PermanentTransportError; оба подкласса TransportError.
    """

    def send(self, request: dict) -> dict:  # pragma: no cover - интерфейс
        raise NotImplementedError


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # заставляет opener вернуть 3xx как HTTPError


class HttpTransport(Transport):
    def __init__(self, base_url: str, api_key: str, *,
                 auth_header: str = "x-api-key",
                 timeout: float = 300.0,
                 allow_loopback_http: bool = True,
                 path: str = "/v1/messages"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._auth_header = auth_header
        self._timeout = timeout
        self._allow_loopback_http = allow_loopback_http
        self._path = path
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    # -- служебное ---------------------------------------------------------
    def __repr__(self) -> str:  # секрет не раскрывается
        return (f"HttpTransport(base_url={self._base_url!r}, "
                f"api_key={'***' if self._api_key else ''}, "
                f"auth_header={self._auth_header!r}, timeout={self._timeout})")

    def _url(self) -> str:
        url = self._base_url + self._path
        parts = urlsplit(url)
        if parts.username or parts.password:
            raise PermanentTransportError("proxy URL must not contain userinfo")
        host = (parts.hostname or "").lower()
        if parts.scheme == "http":
            if not (host in LOOPBACK_HOSTS and self._allow_loopback_http):
                raise PermanentTransportError(
                    "http scheme is allowed only for loopback; use https")
        elif parts.scheme != "https":
            raise PermanentTransportError("proxy URL scheme must be http or https")
        if not parts.hostname:
            raise PermanentTransportError("proxy URL has no host")
        return url

    def _headers(self) -> dict:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
        }
        if self._api_key:
            if self._auth_header == "authorization":
                headers["Authorization"] = f"Bearer {self._api_key}"
            else:
                headers["x-api-key"] = self._api_key
        return headers

    def _sanitize(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return text

    # -- отправка ----------------------------------------------------------
    def send(self, request: dict) -> dict:
        url = self._url()
        data = json.dumps(request, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers=self._headers(), method="POST")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return self._read_success(resp)
        except urllib.error.HTTPError as exc:
            raise self._classify_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, OSError) as exc:
            raise TransientTransportError(
                self._sanitize(f"network error: {exc}")) from exc

    def _read_success(self, resp) -> dict:
        body = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise PermanentTransportError("proxy response exceeds size limit")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # 200 с битым/обрезанным телом — не успех; квалифицируем как
            # временную (сетевой сбой посреди тела) и даём bounded retry.
            raise TransientTransportError(
                self._sanitize(f"invalid JSON in 200 response: {exc}")) from exc
        if not isinstance(parsed, dict):
            raise PermanentTransportError("proxy response is not a JSON object")
        return parsed

    def _classify_http_error(self, exc: urllib.error.HTTPError) -> TransportError:
        status = exc.code
        retry_after: float | None = None
        try:
            raw = exc.headers.get("Retry-After")
        except Exception:  # pragma: no cover - headers отсутствуют
            raw = None
        if raw:
            retry_after = _parse_retry_after(raw)
        detail_body = ""
        try:
            snippet = exc.read(2048)
            detail_body = snippet.decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover - тело уже прочитано
            pass
        detail = self._sanitize(
            f"HTTP {status}: {detail_body[:300]}" if detail_body else f"HTTP {status}")
        if status in _TRANSIENT_STATUS:
            return TransientTransportError(detail, status=status,
                                           retry_after=retry_after)
        return PermanentTransportError(detail, status=status,
                                       retry_after=retry_after)


def _parse_retry_after(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date не парсим: берём только числовые секунды
