"""ZIP-импорт как отдельный слой (не редактор базы!).

Задача слоя: безопасно распаковать архив, разделить записи на аудио
(→ задания расшифровки через engine.create_job) и документы (→ внешний
document_handler клиента). Обработка/публикация документов остаётся на
стороне клиента — транскрибатор не создаёт «второй редактор базы».

Безопасность:
- allowlist расширений (аудио + документы);
- лимиты: число записей, размер записи, суммарный объём, коэффициент
  сжатия (защита от zip-бомбы);
- запрет traversal (абсолютные пути, «..», обратные слэши), симлинков,
  вложенных архивов;
- потоковая запись с подсчётом байт: превышение лимита — запись бракуется.

Allowlist документов синхронизирован с реальной админкой AgroBot
(storage.ts ALLOWED_MIME_TYPES / EXT_BY_MIME): pdf, docx, pptx, xlsx,
doc, ppt, xls, txt, md, csv, json, jpg, jpeg, png, webp. Приёмочный
архив владельца (PDF + PPTX + DOCX + аудио) проходит целиком.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

from . import audio
from .errors import ValidationError, ZipImportError

AUDIO_EXTENSIONS = audio.SUPPORTED_EXTENSIONS
# Синхронизировано с AgroBot admin (kb-docs/storage.ts: EXT_BY_MIME);
# jpeg добавлен как допустимое написание image/jpeg ( multer/ busboy
# определяет MIME по расширению — .jpeg тоже валиден для админки).
DOCUMENT_EXTENSIONS = {
    "pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls",
    "txt", "md", "csv", "json",
    "jpg", "jpeg", "png", "webp",
}
FORBIDDEN_ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "bz2", "xz", "7z", "rar",
                                "tgz", "tar.gz"}
_CHUNK = 64 * 1024


def _check_name(name: str) -> str:
    """Отбрасывает путь: только безопасное имя файла."""
    if not name or name.endswith("/"):
        raise ZipImportError(f"directory entries are not imported: {name!r}")
    if "\\" in name:
        raise ZipImportError(f"backslash in entry name: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith("/"):
        raise ZipImportError(f"absolute path in entry: {name!r}")
    if any(part in ("..", ".") for part in pure.parts):
        raise ZipImportError(f"traversal in entry name: {name!r}")
    if len(pure.parts) > 4:
        raise ZipImportError(f"too deep entry path: {name!r}")
    return pure.name


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def scan_entries(zip_path: Path | str, *, limits: dict | None = None) -> list[dict]:
    """Легковесный список записей архива БЕЗ распаковки и заданий (pass5).

    Возвращает [{name, kind, size}], kind: audio | document | rejected
    (+reason) | directory | symlink. Для выбора ОДНОЙ записи до импорта
    (examples/proxy_check.py): архив с несколькими аудио не выбирается
    молча — выбор остаётся за вызывающей стороной. Те же правила имён,
    что и при импорте (traversal/симлинки/вложенные архивы/лимиты).
    """
    cfg = dict(limits or {})
    cfg.setdefault("max_entries", 200)
    cfg.setdefault("max_member_bytes", 268_435_456)
    zip_path = Path(zip_path)
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ZipImportError(f"not a valid zip file: {exc}") from exc
    out: list[dict] = []
    with archive:
        infos = archive.infolist()
        if len(infos) > cfg["max_entries"]:
            raise ZipImportError(
                f"too many entries: {len(infos)} > {cfg['max_entries']}")
        for info in infos:
            if info.is_dir():
                out.append({"name": info.filename, "kind": "directory",
                            "size": 0})
                continue
            if _is_symlink(info):
                out.append({"name": info.filename, "kind": "symlink",
                            "size": info.file_size})
                continue
            try:
                safe_name = _check_name(info.filename)
            except ZipImportError as exc:
                out.append({"name": info.filename, "kind": "rejected",
                            "reason": str(exc), "size": info.file_size})
                continue
            ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
            if ext in FORBIDDEN_ARCHIVE_EXTENSIONS or safe_name.lower().endswith(
                    tuple(f".{e}" for e in FORBIDDEN_ARCHIVE_EXTENSIONS)):
                out.append({"name": info.filename, "kind": "rejected",
                            "reason": "nested archive", "size": info.file_size})
                continue
            if info.file_size > cfg["max_member_bytes"]:
                out.append({"name": info.filename, "kind": "rejected",
                            "reason": f"member exceeds {cfg['max_member_bytes']} bytes",
                            "size": info.file_size})
                continue
            kind = ("audio" if ext in AUDIO_EXTENSIONS
                    else "document" if ext in DOCUMENT_EXTENSIONS
                    else "rejected")
            entry = {"name": info.filename, "kind": kind,
                     "size": info.file_size}
            if kind == "rejected":
                entry["reason"] = f"unsupported type .{ext}"
            out.append(entry)
    return out


def import_zip(zip_path: Path | str, engine, *, document_handler,
               dest_dir: Path | str, owner_id: str = "local",
               language: str | None = None,
               vocabulary: list[str] | None = None,
               limits: dict | None = None,
               audio_filter=None) -> dict:
    """Импортирует архив. Возвращает манифест-отчёт (и пишет manifest.json).

    document_handler(path: Path, meta: dict) — колбэк клиента для документов;
    исключения колбэка фиксируются в отчёте, импорт продолжается.

    audio_filter(name) -> bool (pass5, опционально): отбор аудио ДО
    распаковки и создания заданий — записи с расширением аудио, для которых
    фильтр вернул False, попадают в манифест как kind="not_selected"
    (не распаковываются, заданий не создают, в лимит суммарного объёма
    не входят, поскольку не извлекаются). Документы отбором не затрагиваются.
    """
    cfg = limits or {}
    max_entries = cfg.get("max_entries", engine.config.max_zip_entries)
    max_total = cfg.get("max_total_bytes", engine.config.max_zip_total_bytes)
    max_member = cfg.get("max_member_bytes", engine.config.max_zip_member_bytes)
    max_ratio = cfg.get("max_ratio", engine.config.max_zip_ratio)

    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ZipImportError(f"not a valid zip file: {exc}") from exc

    entries: list[dict] = []
    total_uncompressed = 0
    audio_jobs: list[dict] = []
    with archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise ZipImportError(
                f"too many entries: {len(infos)} > {max_entries}")
        for info in infos:
            if info.is_dir():
                continue
            if _is_symlink(info):
                entries.append({"name": info.filename, "kind": "rejected",
                                "reason": "symlink"})
                continue
            try:
                safe_name = _check_name(info.filename)
            except ZipImportError as exc:
                entries.append({"name": info.filename, "kind": "rejected",
                                "reason": str(exc)})
                continue
            ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
            if ext in FORBIDDEN_ARCHIVE_EXTENSIONS or safe_name.lower().endswith(
                    tuple(f".{e}" for e in FORBIDDEN_ARCHIVE_EXTENSIONS)):
                entries.append({"name": info.filename, "kind": "rejected",
                                "reason": "nested archive"})
                continue
            if info.file_size > max_member:
                entries.append({"name": info.filename, "kind": "rejected",
                                "reason": f"member exceeds {max_member} bytes"})
                continue
            if info.file_size + total_uncompressed > max_total:
                raise ZipImportError(
                    f"zip total uncompressed size exceeds {max_total} bytes")
            if info.compress_size > 0 and info.file_size / info.compress_size > max_ratio:
                entries.append({"name": info.filename, "kind": "rejected",
                                "reason": f"suspicious compression ratio "
                                          f">{max_ratio}"})
                continue
            if (audio_filter is not None and ext in AUDIO_EXTENSIONS
                    and not audio_filter(info.filename)):
                entries.append({"name": info.filename, "kind": "not_selected"})
                continue

            total_uncompressed += info.file_size

            target = _unique_path(dest_dir, safe_name)
            try:
                written = _extract_member(archive, info, target, max_member)
            except ZipImportError as exc:
                entries.append({"name": info.filename, "kind": "rejected",
                                "reason": str(exc)})
                target.unlink(missing_ok=True)
                continue
            entry = {"name": info.filename, "extracted": target.name,
                     "size": written}
            head = _head_bytes(target)
            sniffed = audio.sniff_mime(head)
            if ext in AUDIO_EXTENSIONS or sniffed in (
                    "audio/ogg", "audio/mpeg", "audio/wav", "audio/mp4"):
                if ext not in AUDIO_EXTENSIONS:
                    target.unlink(missing_ok=True)
                    entries.append({"name": info.filename, "kind": "rejected",
                                    "reason": f"audio content but extension "
                                              f".{ext} not allowed"})
                    continue
                try:
                    created = engine.create_job(
                        target, filename=safe_name, owner_id=owner_id,
                        language=language, vocabulary=vocabulary)
                    entry["kind"] = "audio"
                    entry["job_id"] = created["job_id"]
                    entry["duplicate"] = not created["created"]
                    audio_jobs.append(created)
                except ValidationError as exc:
                    entry["kind"] = "rejected"
                    entry["reason"] = f"audio rejected: {exc}"
                    target.unlink(missing_ok=True)
            elif ext in DOCUMENT_EXTENSIONS:
                entry["kind"] = "document"
                entry["sha256"] = _sha256(target)
                entry["document_mime"] = _document_mime_guess(ext, _head_bytes(target))
                try:
                    if document_handler is not None:
                        document_handler(target, {"name": safe_name,
                                                  "size": written,
                                                  "sha256": entry["sha256"],
                                                  "mime": entry["document_mime"]})
                        entry["handler"] = "ok"
                    else:
                        entry["handler"] = "skipped"
                except Exception as exc:  # noqa: BLE001 - колбэк клиента
                    entry["handler"] = f"error: {exc}"
            else:
                entry["kind"] = "rejected"
                entry["reason"] = f"unsupported type .{ext}"
                target.unlink(missing_ok=True)
            entries.append(entry)

    manifest = {
        "zip": zip_path.name,
        "entries": entries,
        "totals": {
            "entries": len(entries),
            "audio_jobs": len(audio_jobs),
            "documents": sum(1 for e in entries if e["kind"] == "document"),
            "rejected": sum(1 for e in entries if e["kind"] == "rejected"),
            "not_selected": sum(1 for e in entries
                                 if e["kind"] == "not_selected"),
            "uncompressed_bytes": total_uncompressed,
        },
    }
    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _unique_path(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    stem, dot, ext = name.rpartition(".")
    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{stem}-{counter}{dot}{ext}"
        counter += 1
    return candidate


def _extract_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                    target: Path, max_member: int) -> int:
    written = 0
    digest = hashlib.sha256()
    try:
        with archive.open(info) as src, open(target, "wb") as dst:
            while True:
                block = src.read(_CHUNK)
                if not block:
                    break
                written += len(block)
                if written > max_member:
                    raise ZipImportError(
                        f"member exceeded limit during extraction: {info.filename}")
                digest.update(block)
                dst.write(block)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ZipImportError(f"failed to extract {info.filename}: {exc}") from exc
    if written != info.file_size:
        raise ZipImportError(
            f"member size mismatch: {info.filename} "
            f"(declared {info.file_size}, read {written})")
    return written


def _head_bytes(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(32)
    except OSError:
        return b""


# MIME документов как в админке AgroBot (для манифеста и колбэка клиента).
_DOCUMENT_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet",
    "doc": "application/msword",
    "ppt": "application/vnd.ms-powerpoint",
    "xls": "application/vnd.ms-excel",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def _document_mime_guess(ext: str, head: bytes) -> str:
    """MIME по расширению + лёгкая проверка контейнера по магическим байтам.

    Расхождение не бракует запись (окончательную проверку делает админка
    AgroBot при загрузке), но помечается в манифесте суффиксом '?'.
    """
    mime = _DOCUMENT_MIME_BY_EXT.get(ext)
    if mime is None:
        return "application/octet-stream"
    ok = False
    if ext == "pdf":
        ok = head.startswith(b"%PDF")
    elif ext in {"docx", "pptx", "xlsx"}:
        ok = head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    elif ext in {"doc", "ppt", "xls"}:
        ok = head[:4] == b"\xd0\xcf\x11\xe0"
    elif ext == "png":
        ok = head[:4] == b"\x89PNG"
    elif ext == "jpg" or ext == "jpeg":
        ok = head[:3] == b"\xff\xd8\xff"
    elif ext == "webp":
        ok = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    else:  # текстовые форматы — без строгой проверки
        ok = b"\x00" not in head
    return mime if ok else mime + "?"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()
