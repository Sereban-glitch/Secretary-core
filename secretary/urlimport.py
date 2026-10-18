"""Безопасная потоковая загрузка файла по HTTPS.

Модуль предназначен для больших аудиофайлов и ZIP из Google Drive или
обычной прямой ссылки. Файл пишется частями, не помещается целиком в RAM.
Каждый переход проверяется против SSRF; соединение привязывается к уже
проверенному IP, чтобы DNS не мог подмениться между проверкой и запросом.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import shutil
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from .errors import UrlImportError

_DRIVE_ID = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_SAFE_NAME = re.compile(r"[^\w.() -]+", re.UNICODE)
_REDIRECTS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class DownloadResult:
    path: str
    filename: str
    size: int
    sha256: str
    content_type: str
    source_host: str


def normalize_google_drive_url(url: str) -> str:
    """Преобразует публичную Drive-ссылку в адрес скачивания."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in {
            "drive.google.com", "docs.google.com", "drive.usercontent.google.com"}:
        return url
    match = _DRIVE_ID.search(parts.path)
    file_id = match.group(1) if match else parse_qs(parts.query).get("id", [""])[0]
    if not file_id:
        return url
    return ("https://drive.usercontent.google.com/download?export=download&confirm=t&id="
            + quote(file_id, safe=""))


def _safe_filename(value: str, fallback: str = "download.bin") -> str:
    value = Path(unquote(value).replace("\\", "/")).name.strip().strip(".")
    value = _SAFE_NAME.sub("_", value)
    return value[:180] or fallback


def _filename_from_headers(headers, url: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if not match:
        match = re.search(r'filename="?([^";]+)', disposition, re.I)
    candidate = match.group(1) if match else Path(urlsplit(url).path).name
    return _safe_filename(candidate)


def _validate_ip(text: str) -> str:
    ip = ipaddress.ip_address(text)
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise UrlImportError("ссылка ведёт во внутреннюю или служебную сеть")
    return str(ip)


def _resolve_public(host: str, port: int, resolver=socket.getaddrinfo) -> str:
    try:
        rows = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UrlImportError("не удалось определить адрес сервера") from exc
    addresses = []
    for row in rows:
        addresses.append(_validate_ip(row[4][0]))
    if not addresses:
        raise UrlImportError("сервер ссылки не найден")
    return addresses[0]


def _validate_url(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UrlImportError("разрешены только защищённые HTTPS-ссылки")
    if parts.username or parts.password:
        raise UrlImportError("логин и пароль внутри ссылки запрещены")
    if not parts.hostname:
        raise UrlImportError("в ссылке отсутствует имя сервера")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UrlImportError("некорректный порт в ссылке") from exc
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.hostname.lower(), port, path


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _open_once(url: str, *, timeout: float, resolver=socket.getaddrinfo):
    host, port, path = _validate_url(url)
    pinned_ip = _resolve_public(host, port, resolver)
    conn = _PinnedHTTPSConnection(host, port, pinned_ip, timeout)
    conn.request("GET", path, headers={
        "Host": host if port == 443 else f"{host}:{port}",
        "User-Agent": "Secretary-Core/1.0",
        "Accept": "application/octet-stream,*/*;q=0.8",
        "Connection": "close",
    })
    return conn, conn.getresponse()


def download_url(url: str, dest_dir: str | Path, *, max_bytes: int,
                 max_redirects: int = 4, timeout: float = 120.0,
                 min_free_after_bytes: int = 536_870_912,
                 opener=_open_once, resolver=socket.getaddrinfo) -> DownloadResult:
    """Потоково скачивает URL и атомарно публикует локальный файл."""
    if max_bytes <= 0 or min_free_after_bytes < 0 or max_redirects < 0:
        raise UrlImportError("лимиты загрузки должны быть положительными")
    current = normalize_google_drive_url(url.strip())
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest).free < max_bytes + min_free_after_bytes:
        raise UrlImportError("недостаточно свободного места для безопасной загрузки")

    conn = response = None
    for hop in range(max_redirects + 1):
        _validate_url(current)
        conn, response = opener(current, timeout=timeout, resolver=resolver)
        if response.status in _REDIRECTS:
            location = response.getheader("Location")
            response.read()
            conn.close()
            if not location:
                raise UrlImportError("сервер вернул перенаправление без адреса")
            if hop == max_redirects:
                raise UrlImportError("слишком много перенаправлений")
            current = urljoin(current, location)
            continue
        break
    assert conn is not None and response is not None
    if response.status < 200 or response.status >= 300:
        response.read(4096)
        conn.close()
        raise UrlImportError(f"сервер вернул ошибку HTTP {response.status}")

    length = response.getheader("Content-Length")
    if length:
        try:
            if int(length) > max_bytes:
                conn.close()
                raise UrlImportError("файл превышает установленный лимит")
        except ValueError as exc:
            conn.close()
            raise UrlImportError("сервер сообщил некорректный размер файла") from exc

    filename = _filename_from_headers(response.headers, current)
    target = dest / filename
    if target.exists():
        stem, suffix = target.stem, target.suffix
        tag = hashlib.sha256(current.encode()).hexdigest()[:8]
        target = dest / f"{stem}-{tag}{suffix}"
        counter = 2
        while target.exists() or target.with_name(target.name + ".part").exists():
            target = dest / f"{stem}-{tag}-{counter}{suffix}"
            counter += 1
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    size = 0
    content_type = response.getheader("Content-Type", "application/octet-stream")
    try:
        with partial.open("xb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UrlImportError("файл превысил лимит во время загрузки")
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if size == 0:
            raise UrlImportError("сервер вернул пустой файл")
        if content_type.lower().startswith("text/html"):
            raise UrlImportError(
                "вместо файла получена веб-страница; проверьте публичный доступ по ссылке")
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    return DownloadResult(str(target), target.name, size, digest.hexdigest(),
                          content_type.split(";", 1)[0], urlsplit(current).hostname or "")
