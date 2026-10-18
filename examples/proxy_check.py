#!/usr/bin/env python3
"""Утилита проверки одной живой аудиозаписи через приватный прокси (pass5).

Назначение: владелец/проверяющий запускает её В СВОЕЙ среде, где доступен
приватный прокси. В среде разработки прокси НЕ доступен — живой успех
здесь не проверяется и не имитируется (см. docs/PROXY.md).

Подкоманды:
  doctor          — проверка готовности среды (ffmpeg/ffprobe, место,
                    настройки прокси, /proc для замера памяти);
  run             — прогон ОДНОЙ записи: dry-run (по умолчанию, без сети)
                    или live (только с двойным подтверждением; в прокси
                    уходит ТОЛЬКО локально вырезанный короткий фрагмент
                    записи — по умолчанию первые 60 секунд);
  replay          — возобновление незавершённых заданий последнего запуска
                    (--retry-failed — явный перезапуск упавших);
  review-package  — пересборка пакета материалов запуска из сохранённых
                    артефактов (без новых обращений к транспорту).

Гарантии (наследуются из ядра, pass2–pass4):
- allowlist форматов, лимиты размера/числа файлов/длительности, защита от
  path traversal, симлинков и zip-бомбы — слой secretary.zipimport;
- bounded retry (без бесконечных повторов), временные/постоянные ошибки
  различаются;
- строгая область одного запуска: обрабатываются ТОЛЬКО job_id из
  check-run.json текущего запуска;
- честные статусы: transcription_status по всем заданиям запуска,
  workflow_status по факту (код возврата ему соответствует);
- публикация: утилита НЕ интегрирована с AgroBot — запросов к нему нет
  ВООБЩЕ, ни в каком режиме;
- секреты: ключ прокси читается только из окружения/env-файла, никогда
  не попадает в отчёты, логи и пакет материалов.

Обезличенный отчёт (report.json / report.md): раздельное время этапов,
пиковая и средняя память (self + дочерние процессы, RSS из /proc; при
недоступности /proc — честное "unavailable"), статусы, счётчики,
длительность записи, размеры и контрольные суммы (sha256) файлов.
По умолчанию НИ ОДНОГО символа расшифровки: полный текст — только
в пакете материалов (review/, права 0700/0600) или в отчёте с явным
флагом --include-transcript (такой отчёт закрывается правами 0600).

Dry-run не делает НИ ОДНОГО сетевого обращения (транспорт-двойник из
tests/fakes.py). Live требует ОБЕИХ подтверждений: переменная окружения
SECRETARY_LIVE_CONFIRM=1 И флаг --live-yes.

Коды возврата: 0 — успех; 1 — ошибка выбора/аргументов; 2 — live
заблокирован (нет двойного подтверждения или настроек прокси); 3 —
неготовность среды (doctor) либо незавершённая расшифровка.

Демо-ручки ТОЛЬКО для dry-run (тесты/демо упавшего прокси, на live не
влияют): SECRETARY_FAKE_TRANSPORT_FAIL=1 — каждый запрос падает постоянно;
SECRETARY_FAKE_TRANSPORT_TRANSIENT=N — первые N запросов падают временно.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # репозиторий
sys.path.insert(0, str(Path(__file__).parent))         # examples/

from secretary import audio as audio_mod                   # noqa: E402
from secretary.config import SecretaryConfig              # noqa: E402
from secretary.engine import TranscriptionEngine          # noqa: E402
from secretary.errors import AudioError, ConfigError, ValidationError  # noqa: E402
from secretary.errors import ZipImportError               # noqa: E402
from secretary.result import build_result, render_markdown  # noqa: E402
from secretary.store import Store                         # noqa: E402
from secretary.zipimport import import_zip, scan_entries  # noqa: E402
from agrobot_orchestrator import compute_transcription_status  # noqa: E402

TOOL = "proxy_check"
TOOL_VERSION = 2
RUN_FILE = "check-run.json"
EXIT_OK, EXIT_USAGE, EXIT_LIVE_BLOCKED, EXIT_FAILED = 0, 1, 2, 3
LIVE_CONFIRM_ENV = "SECRETARY_LIVE_CONFIRM"
# live-фрагмент: в прокси уходит только локально вырезанный кусок записи
LIVE_FRAGMENT_DEFAULT_SECONDS = 60
LIVE_FRAGMENT_MIN_SECONDS = 1
LIVE_FRAGMENT_MAX_SECONDS = 600


# ── замер памяти (RSS self + дочерние процессы) ─────────────────────────
class MemorySampler:
    """Поток-сэмплер RSS из /proc; при недоступности /proc — available=False.

    Дочерние процессы (ffmpeg/ffprobe) учитываются сканированием /proc/*/stat
    по полю ppid. Чтение локальное, ничего не отправляет наружу.
    """

    def __init__(self, interval: float = 0.2, *,
                 status_reader=None, children_reader=None):
        self.interval = interval
        self.samples: list[tuple[float, int, int]] = []  # (t, self_kb, kids_kb)
        self.available = True
        self._read_status = status_reader or self._proc_status_kb
        self._read_children = children_reader or self._proc_children_kb
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        t0 = time.monotonic()
        while True:
            try:
                mine = self._read_status(os.getpid())
                kids = self._read_children(os.getpid())
            except (OSError, ValueError):
                self.available = False
                return
            if mine is None:
                self.available = False
                return
            self.samples.append(
                (round(time.monotonic() - t0, 3), mine, kids or 0))
            if self._stop.wait(self.interval):
                return

    # ── чтение /proc ────────────────────────────────────────────────────
    @staticmethod
    def _proc_status_kb(pid: int) -> int | None:
        try:
            with open(f"/proc/{pid}/status", "r", encoding="ascii",
                      errors="replace") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
        except OSError:
            return None
        return None

    @staticmethod
    def _proc_children_kb(parent_pid: int) -> int:
        total = 0
        try:
            entries = os.listdir("/proc")
        except OSError:
            return 0
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            # ppid — поле после закрывающей скобки comm (3-е поле stat)
            close = raw.rfind(b")")
            if close < 0:
                continue
            fields = raw[close + 2:].split()
            if len(fields) < 2:
                continue
            try:
                if int(fields[1]) == parent_pid:
                    kb = MemorySampler._proc_status_kb(int(name))
                    if kb:
                        total += kb
            except ValueError:
                continue
        return total

    def report(self) -> dict:
        if not self.available:
            return {"available": False, "peak_rss_kb": "unavailable",
                    "avg_rss_kb": "unavailable", "samples": 0,
                    "note": "/proc недоступен — замер памяти честно "
                            "отсутствует"}
        totals = [mine + kids for _, mine, kids in self.samples]
        return {
            "available": True,
            "samples": len(self.samples),
            "peak_rss_kb": max(totals) if totals else 0,
            "avg_rss_kb": round(sum(totals) / len(totals)) if totals else 0,
            "note": "RSS процесса утилиты + дочерних ffmpeg/ffprobe "
                    "(self + children, пики по сэмплам)",
        }


class _CountingTransport:
    """Обёртка транспорта: считает запросы и время в транспорте (сеть)."""

    def __init__(self, inner):
        self.inner = inner
        self.sent = 0
        self.send_seconds = 0.0

    def send(self, request: dict) -> dict:
        started = time.monotonic()
        try:
            self.sent += 1
            return self.inner.send(request)
        finally:
            self.send_seconds += time.monotonic() - started


# ── выбор записи ──────────────────────────────────────────────────────────
def list_audio_candidates(zip_path: Path, config: SecretaryConfig) -> list[dict]:
    """Только аудио-записи архива (без распаковки)."""
    limits = {"max_entries": config.max_zip_entries,
              "max_member_bytes": config.max_zip_member_bytes}
    entries = scan_entries(zip_path, limits=limits)
    return [e for e in entries if e["kind"] == "audio"]


def resolve_selection(args, config: SecretaryConfig) -> dict:
    """Решает, ЧТО импортировать. Архив с несколькими аудио не выбирается
    молча: требуется --select/--index или --all (иначе выход 1, заданий нет).
    """
    source = Path(args.audio)
    if source.is_file() and not source.name.lower().endswith(".zip"):
        ext = source.suffix.lstrip(".").lower()
        if ext not in audio_mod.SUPPORTED_EXTENSIONS:
            print(f"файл .{ext} не входит в allowlist аудио "
                  f"({', '.join(sorted(audio_mod.SUPPORTED_EXTENSIONS))})")
            raise SystemExit(EXIT_USAGE)
        return {"type": "file", "path": source,
                "selected": [source.name], "zip": None}
    if not source.is_file():
        print(f"архив/файл не найден: {source}")
        raise SystemExit(EXIT_USAGE)
    try:
        candidates = list_audio_candidates(source, config)
    except ZipImportError as exc:
        print(f"архив не читается: {exc}")
        raise SystemExit(EXIT_USAGE)
    if not candidates:
        print("в архиве нет аудиозаписей из allowlist; doctor покажет "
              "поддерживаемые форматы")
        raise SystemExit(EXIT_USAGE)
    names = [c["name"] for c in candidates]
    if len(candidates) > 1 and not (args.select or args.index or args.all):
        listing = "\n".join(f"  {i + 1}. {n}" for i, n in enumerate(names))
        print(f"в архиве {len(candidates)} аудиозаписей — выбор неочевиден:\n"
              f"{listing}\nукажите --select <имя> или --index <номер>, "
              f"либо --all для обработки всех")
        raise SystemExit(EXIT_USAGE)
    if args.all:
        chosen = names
    elif args.index is not None:
        if not (1 <= args.index <= len(candidates)):
            print(f"--index вне диапазона 1..{len(candidates)}")
            raise SystemExit(EXIT_USAGE)
        chosen = [names[args.index - 1]]
    elif args.select:
        if args.select not in names:
            print(f"--select {args.select!r} нет среди аудио: "
                  f"{', '.join(names)}")
            raise SystemExit(EXIT_USAGE)
        chosen = [args.select]
    else:
        chosen = names  # единственная запись
    return {"type": "zip", "path": source, "selected": chosen, "zip": source}


# ── транспорты ────────────────────────────────────────────────────────────
def build_dry_run_transport(env: dict):
    """Транспорт-двойник без сети + демо-ручки отказов (dry-run only)."""
    from tests.fakes import FakeTransport
    from secretary.errors import (PermanentTransportError,
                                  TransientTransportError)

    if env.get("SECRETARY_FAKE_TRANSPORT_FAIL") == "1":
        def _fail(request: dict) -> dict:
            raise PermanentTransportError(
                "demo: simulated proxy failure (SECRETARY_FAKE_TRANSPORT_FAIL)")
        return FakeTransport(responder=_fail)

    transient_raw = env.get("SECRETARY_FAKE_TRANSPORT_TRANSIENT")
    if transient_raw:
        try:
            left = max(0, int(transient_raw))
        except ValueError:
            left = 0
        if left > 0:
            base = FakeTransport()

            def _flaky(request: dict) -> dict:
                nonlocal left
                if left > 0:
                    left -= 1
                    raise TransientTransportError(
                        "demo: transient failure "
                        "(SECRETARY_FAKE_TRANSPORT_TRANSIENT)")
                return base._default_responder(request)
            return FakeTransport(responder=_flaky)
    return FakeTransport()


def build_engine(config: SecretaryConfig, transport) -> TranscriptionEngine:
    return TranscriptionEngine(config, transport, Store(config.db_path))


def load_config(args) -> tuple[SecretaryConfig, dict]:
    env_file = getattr(args, "env_file", None)
    config = SecretaryConfig.from_env(env_file=env_file)
    merged: dict[str, str] = {}
    if env_file and os.path.isfile(env_file):
        from secretary.config import _read_env_file
        merged.update(_read_env_file(env_file))
    merged.update(os.environ)
    return config, merged


def resolve_live(args, config: SecretaryConfig, env: dict) -> bool:
    """Live-режим только с ДВОЙНЫМ подтверждением: env + флаг."""
    if not args.live:
        return False
    env_ok = env.get(LIVE_CONFIRM_ENV) == "1"
    flag_ok = bool(getattr(args, "live_yes", False))
    if not (env_ok and flag_ok):
        print("LIVE ЗАБЛОКИРОВАН: нужно ОБА подтверждения —\n"
              f"  1) переменная окружения {LIVE_CONFIRM_ENV}=1\n"
              "  2) флаг --live-yes в команде\n"
              "ничего не запущено, заданий не создано, сети нет.")
        raise SystemExit(EXIT_LIVE_BLOCKED)
    if not config.proxy_base_url or not config.model:
        print("LIVE ЗАБЛОКИРОВАН: не заданы SECRETARY_PROXY_BASE_URL и "
              "SECRETARY_MODEL (см. OWNER-GUIDE-RU.md, шаг живого теста). "
              "Ничего не запущено.")
        raise SystemExit(EXIT_LIVE_BLOCKED)
    return True


# ── импорт ────────────────────────────────────────────────────────────────
def do_import(engine: TranscriptionEngine, selection: dict, *,
              owner: str, language: str | None,
              vocabulary: list[str], dest: Path) -> dict:
    if selection["type"] == "file":
        created = engine.create_job(Path(selection["path"]),
                                    owner_id=owner, language=language,
                                    vocabulary=vocabulary,
                                    filename=selection["selected"][0])
        entries = [{"name": selection["selected"][0], "kind": "audio",
                    "job_id": created["job_id"]}]
        return {"entries": entries,
                "totals": {"entries": 1, "audio_jobs": 1, "documents": 0,
                           "rejected": 0, "not_selected": 0,
                           "uncompressed_bytes": 0},
                "manifest_path": None}
    chosen = set(selection["selected"])
    manifest = import_zip(
        selection["path"], engine, document_handler=None,
        dest_dir=dest / "imports", owner_id=owner, language=language,
        vocabulary=vocabulary,
        audio_filter=(lambda name: name in chosen))
    return manifest


def parse_vocabulary(value: str | None, file_name: str | None) -> list[str]:
    """Собирает короткий предметный словарь из CSV и текстового файла.

    В файле один термин на строку; пустые строки и строки с # пропускаются.
    Порядок сохраняется, повторы без учёта регистра удаляются.
    """
    raw: list[str] = []
    if value:
        raw.extend(value.split(","))
    if file_name:
        path = Path(file_name)
        if not path.is_file():
            raise ValueError(f"файл словаря не найден: {path}")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                raw.append(line)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        term = " ".join(item.strip().split())
        if not term:
            continue
        if len(term) > 100:
            raise ValueError(f"слишком длинный термин словаря: {term[:40]}…")
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            result.append(term)
    if len(result) > 100:
        raise ValueError("в словаре больше 100 терминов")
    return result


# ── прогон ────────────────────────────────────────────────────────────────
def run_pipeline(engine: TranscriptionEngine, job_ids: list[str],
                 *, sampler: MemorySampler) -> dict:
    stats = engine.run_pending(job_ids=job_ids)
    statuses: list[str | None] = []
    for job_id in job_ids:
        job = engine.store.find_job(job_id)
        statuses.append(None if job is None else job["status"])
    status, counts = compute_transcription_status(job_ids, statuses)
    return {"stats": stats, "status": status, "counts": counts}


def sha256_of(path: Path) -> str:
    """Контрольная сумма файла (локально, блоками по 1 МиБ)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_zip_member(zip_path: Path, name: str, target: Path,
                        max_bytes: int) -> None:
    """Извлекает ОДНУ выбранную запись архива во временный файл.

    Те же ограничения, что при импорте: лимит размера, отказ от симлинков,
    сверка заявленного и прочитанного размеров. Без распаковки всего
    архива — только выбранная запись.
    """
    with zipfile.ZipFile(zip_path) as archive:
        info = next((i for i in archive.infolist()
                     if i.filename == name and not i.is_dir()), None)
        if info is None:
            raise ZipImportError(f"entry not found in archive: {name}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ZipImportError(f"symlink member rejected: {name}")
        if info.file_size > max_bytes:
            raise ZipImportError(
                f"member exceeds limit during extraction: {name}")
        written = 0
        try:
            with archive.open(info) as src, open(target, "wb") as dst:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    written += len(block)
                    if written > max_bytes:
                        raise ZipImportError(
                            f"member exceeded limit during extraction: {name}")
                    dst.write(block)
        except zipfile.BadZipFile as exc:
            raise ZipImportError(f"failed to extract {name}: {exc}") from exc
        if written != info.file_size:
            raise ZipImportError(
                f"member size mismatch: {name} "
                f"(declared {info.file_size}, read {written})")


def cut_live_fragment(selection: dict, *, seconds: int,
                      config: SecretaryConfig, workdir: Path) -> Path:
    """Live: локально вырезает короткий фрагмент выбранной записи.

    До прокси доходит ТОЛЬКО этот файл: ffmpeg обрезает первые `seconds`
    секунд (моно, 16 кГц, 32 кбит/с mp3), и задание создаётся из фрагмента.
    Исходная запись не изменяется и целиком не отправляется. Временный
    файл извлечения из ZIP убирается сразу; сам фрагмент удаляет
    вызывающий после импорта (у задания появляется своя копия).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    source = Path(selection["path"])
    extracted: Path | None = None
    if selection["type"] == "zip":
        extracted = workdir / "live-source.tmp"
        _extract_zip_member(source, selection["selected"][0], extracted,
                            config.max_zip_member_bytes)
        input_path = extracted
    else:
        input_path = source
    fragment = workdir / "live-fragment.mp3"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(input_path),
             "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000",
             "-codec:a", "libmp3lame", "-b:a", "32k", str(fragment)],
            capture_output=True, text=True,
            timeout=config.ffmpeg_timeout_seconds)
    finally:
        if extracted is not None:
            extracted.unlink(missing_ok=True)
    if proc.returncode != 0 or not fragment.is_file():
        fragment.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg не смог вырезать фрагмент: "
                           + (proc.stderr or "").strip()[:300])
    return fragment


def collect_jobs(engine: TranscriptionEngine, job_ids: list[str], *,
                 include_text: bool = False) -> list[dict]:
    """Итоги по заданиям. Текст расшифровки — только при include_text
    (явный флаг --include-transcript); по умолчанию в отчёт не попадает
    ни одного символа расшифровки — только счётчики."""
    jobs = []
    for job_id in job_ids:
        job = engine.store.find_job(job_id)
        if job is None:
            jobs.append({"job_id": job_id, "status": "missing",
                         "segments": {}, "transcript_chars": 0,
                         "transcript_lines": 0, "duration_seconds": None,
                         "source_bytes": None, "source_sha256": None,
                         "filename": None})
            continue
        segments = engine.store.list_segments(job_id)
        done = sum(1 for s in segments if s["status"] == "done")
        failed = sum(1 for s in segments if s["status"] == "failed")
        transcript = job.get("transcript") or ""
        entry = {
            "job_id": job_id,
            "filename": job.get("source_filename"),
            "status": job["status"],
            "segments": {"total": len(segments), "done": done,
                         "failed": failed},
            "transcript_chars": len(transcript),
            "transcript_lines": len(transcript.splitlines()),
            "duration_seconds": round(job["source_duration"], 1),
            "source_bytes": job["source_size"],
            "source_sha256": job["source_sha256"],
        }
        if include_text:
            entry["transcript"] = transcript
        jobs.append(entry)
    return jobs


def workflow_of(transcription_status: str) -> str:
    if transcription_status == "complete":
        return "complete"
    if transcription_status == "partial":
        return "partial"
    return "failed"


def secure_tree(root: Path) -> None:
    """Каталоги 0700, файлы 0600 — для дерева с полным текстом/аудио."""
    if not root.exists():
        return
    try:
        root.chmod(0o700)
        for path in root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
    except OSError as exc:  # права — best effort, не валить прогон
        print(f"      предупреждение: права не применены ({exc})")


def write_csv(path: Path, samples: list[tuple[float, int, int]]) -> None:
    with open(path, "w", encoding="ascii", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t_seconds", "rss_self_kb", "rss_children_kb"])
        for row in samples:
            writer.writerow(row)


def build_report(*, run_id: str, mode: str, source: dict, selection: dict,
                 jobs: list[dict], transcription: dict, timings: dict,
                 memory: dict, transport_requests: int, environment: dict,
                 replay: dict | None = None,
                 include_transcript: bool = False) -> dict:
    workflow = workflow_of(transcription["status"])
    report = {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "run_id": run_id,
        "mode": mode,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,
        "selection": {"type": selection.get("type"),
                      "selected": selection.get("selected", [])},
        "jobs": jobs,
        "transcription_status": transcription["status"],
        "transcription": transcription["counts"],
        "workflow_status": workflow,
        "exit_code": {"complete": EXIT_OK, "partial": EXIT_FAILED,
                      "failed": EXIT_FAILED}[workflow],
        "timings": {k: round(v, 3) for k, v in timings.items()},
        "memory": memory,
        "transport": {"requests": transport_requests,
                      "network_in_dry_run": False if mode == "dry-run"
                      else None},
        "environment": environment,
        "agrobot": {"publication": "none",
                    "requests": 0,
                    "note": "утилита не интегрирована с AgroBot: "
                            "публикации и запросов нет ни в одном режиме"},
        "transcript_included": include_transcript,
        "anonymization": {"api_key": "не включается никогда",
                          "proxy_url": "полный URL не включается "
                                       "(в live — только хост)",
                          "transcript_text": (
                              "включён полностью: явный флаг "
                              "--include-transcript, отчёт закрыт правами "
                              "0600" if include_transcript else
                              "не включается ни одного символа; полный "
                              "текст — только в пакете материалов "
                              "review/ (права 0700/0600)")},
    }
    if replay:
        report["replay"] = replay
    if mode == "live" and source.get("proxy_host"):
        report["proxy"] = {"host": source["proxy_host"]}  # хост, не URL
    if workflow == "failed":
        report["reason"] = ("ни один сегмент не расшифрован; подробности — "
                            "в предупреждениях заданий и журнале запуска")
    return report


def render_report_markdown(report: dict) -> str:
    src = report["source"]
    mem = report["memory"]
    timings = report["timings"]
    detail = ", ".join(
        f"{k} {v}" for k, v in report["transcription"].items()
        if v and k != "expected") or "аудио не ожидалось"
    lines = [
        f"# Проверка записи: {src.get('filename', '—')}",
        "",
        f"- режим: **{report['mode']}**",
        f"- итог: **{report['workflow_status']}** "
        f"(код возврата {report['exit_code']})",
        f"- расшифровка: {report['transcription_status']} — {detail}",
        "",
        "## Записи",
        "",
        "| задание | статус | сегменты (готово/всего) | длительность, с | "
        "символов текста |",
        "|---|---|---|---|---|",
    ]
    for job in report["jobs"]:
        segs = job["segments"]
        duration = job.get("duration_seconds")
        duration_text = "—" if duration is None else f"{duration:g}"
        lines.append(
            f"| `{job['job_id'][:8]}` | {job['status']} | "
            f"{segs.get('done', 0)}/{segs.get('total', 0)} | "
            f"{duration_text} | {job['transcript_chars']} |")
    lines += [
        "",
        "## Файлы и контрольные суммы",
        "",
        f"- источник: {src.get('filename', '—')} — "
        f"{src.get('size_bytes', '—')} байт, "
        f"sha256 `{src.get('sha256', '—')}`",
    ]
    frag = src.get("live_fragment")
    if frag:
        lines.append(
            f"- live-фрагмент (в прокси уходит только он): "
            f"{frag.get('duration_seconds')} с (запрошено "
            f"{frag.get('requested_seconds')} с), "
            f"{frag.get('size_bytes')} байт, "
            f"sha256 `{frag.get('sha256')}`")
    for job in report["jobs"]:
        if job.get("source_sha256"):
            lines.append(
                f"- задание `{job['job_id'][:8]}`"
                + (f" ({job['filename']})" if job.get("filename") else "")
                + f": {job.get('source_bytes')} байт, "
                f"sha256 `{job['source_sha256']}`, длительность "
                f"{job.get('duration_seconds')} с")
    lines += [
        "",
        "## Время и память",
        "",
        f"- общее время: {timings.get('total_seconds')} с",
        f"- выбор записи и контрольная сумма: "
        f"{timings.get('select_seconds')} с",
        f"- вырезание live-фрагмента: {timings.get('fragment_seconds')} с",
        f"- импорт: {timings.get('import_seconds')} с",
        f"- расшифровка (локально + сеть): "
        f"{timings.get('transcribe_seconds')} с",
        f"  - из них транспорт (сеть): {timings.get('network_seconds')} с",
        f"- сборка отчёта: {timings.get('report_seconds')} с",
        f"- пиковая память: {mem.get('peak_rss_kb')} КиБ "
        f"(self + дочерние ffmpeg/ffprobe)",
        f"- средняя память: {mem.get('avg_rss_kb')} КиБ",
        "",
        "## Конфиденциальность",
        "",
        "- AgroBot: публикация не выполнялась, запросов нет.",
        "- Ключ прокси и полный URL в отчёт не включаются.",
    ]
    if report.get("transcript_included"):
        lines.append("- Полный текст расшифровки включён в этот отчёт ЯВНЫМ "
                     "флагом --include-transcript; файл закрыт правами "
                     "0600.")
        for job in report["jobs"]:
            if job.get("transcript"):
                lines += ["", f"### Расшифровка `{job['job_id'][:8]}`",
                          "", job["transcript"]]
    else:
        lines.append("- Текста расшифровки в этом отчёте нет совсем: ни "
                     "одного символа. Полный текст — только в пакете "
                     "материалов (review/, права 0700/0600).")
    return "\n".join(lines) + "\n"


def write_report(dest: Path, report: dict) -> None:
    """Отчёт: без текста расшифровки — открытый для передачи (0644);
    с текстом (явный флаг) — закрытый (0600)."""
    mode = 0o600 if report.get("transcript_included") else 0o644
    for name in ("report.json", "report.md"):
        path = dest / name
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
            if name.endswith(".json") else render_report_markdown(report),
            encoding="utf-8")
        try:
            path.chmod(mode)
        except OSError:
            pass


def build_review_package(dest: Path, engine: TranscriptionEngine,
                         run_state: dict, report: dict | None = None) -> Path:
    """Пакет материалов: отчёты + полный текст (права 0700/0600)."""
    run_id = run_state["run_id"]
    review = dest / f"review-{run_id}"
    if review.exists():
        shutil.rmtree(review)
    (review / "jobs").mkdir(parents=True)
    if report is None:
        payload = (dest / "report.json")
        report = json.loads(payload.read_text(encoding="utf-8")) \
            if payload.is_file() else {}
    (review / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = dest / "report.md"
    if md.is_file():
        shutil.copyfile(md, review / "report.md")
    manifest = run_state.get("manifest_path")
    if manifest and Path(manifest).is_file():
        shutil.copyfile(manifest, review / "manifest.json")
    for job_id in run_state["job_ids"]:
        job = engine.store.find_job(job_id)
        if job is None or job["status"] not in ("completed", "partial"):
            continue
        result = build_result(job, engine.store.list_segments(job_id))
        (review / "jobs" / f"transcript-{job_id[:8]}.md").write_text(
            render_markdown(result), encoding="utf-8")
    (review / RUN_FILE).write_text(
        json.dumps(run_state, ensure_ascii=False, indent=2),
        encoding="utf-8")
    csv_path = dest / "memory-samples.csv"
    if csv_path.is_file():
        shutil.copyfile(csv_path, review / "memory-samples.csv")
    doctor = dest / "doctor.json"
    if doctor.is_file():
        shutil.copyfile(doctor, review / "doctor.json")
    (review / "package-manifest.json").write_text(
        json.dumps(_package_manifest(review, run_state), ensure_ascii=False,
                   indent=2), encoding="utf-8")
    secure_tree(review)
    return review


def _package_manifest(review: Path, run_state: dict) -> dict:
    """Опись пакета: каждый файл с размером и sha256 (+ чего нет и почему).

    Сама опись в свой список не входит (подписать собственную контрольную
    сумму невозможно); полнота пакета проверяется сверкой файлов и сумм.
    """
    purposes = {"report.json": "обезличенный отчёт (JSON)",
                "report.md": "обезличенный отчёт (Markdown)",
                "manifest.json": "манифест импорта ZIP",
                "check-run.json": "состояние запуска (job_ids, пути)",
                "memory-samples.csv": "сэмплы RSS (t, self, children)",
                "doctor.json": "итоги проверки среды (doctor)"}
    files = []
    for path in sorted(review.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(review).as_posix()
        files.append({"path": rel, "size_bytes": path.stat().st_size,
                      "sha256": sha256_of(path),
                      "purpose": purposes.get(
                          rel, "полный текст расшифровки задания"
                          if rel.startswith("jobs/") else "материал запуска")})
    absent = []
    if not (review / "manifest.json").is_file():
        absent.append({"path": "manifest.json",
                       "reason": "источник — одиночный файл, не ZIP-архив"})
    if not list((review / "jobs").glob("transcript-*.md")):
        absent.append({"path": "jobs/transcript-*.md",
                       "reason": "нет завершённых заданий с текстом"})
    if not (review / "doctor.json").is_file():
        absent.append({"path": "doctor.json",
                       "reason": "doctor не выполнялся в этот каталог"})
    return {"package": review.name, "run_id": run_state.get("run_id"),
            "mode": run_state.get("mode"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": files, "absent": absent,
            "note": "опись с контрольными суммами каждого файла; полнота "
                    "пакета проверяется сверкой файлов и sha256"}


def environment_report(config: SecretaryConfig) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    def _version(binary: str | None) -> str | None:
        if not binary:
            return None
        try:
            out = subprocess.run([binary, "-version"], capture_output=True,
                                  text=True, timeout=15)
            return out.stdout.splitlines()[0][:120] if out.stdout else None
        except (OSError, subprocess.SubprocessError):
            return None

    try:
        free = shutil.disk_usage(Path(config.jobs_dir)).free
    except OSError:
        free = None
    return {
        "ffmpeg": _version(ffmpeg),
        "ffprobe": _version(ffprobe),
        "disk_free_bytes": free,
        "disk_budget_bytes": config.disk_budget_bytes,
        "proc_available": Path("/proc/self/status").is_file(),
        "python": sys.version.split()[0],
    }


# ── doctor ────────────────────────────────────────────────────────────────
def cmd_doctor(args) -> int:
    try:
        config, env = load_config(args)
    except ConfigError as exc:
        print("=== doctor: проверка среды ===")
        print(f"  [FAIL] настройки: {exc}")
        print("  dry-run: НЕ ГОТОВ (исправьте переменные SECRETARY_*)")
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "doctor.json").write_text(
            json.dumps({"dry_run_ready": False, "live_ready": False,
                        "config_error": str(exc)}, ensure_ascii=False,
                       indent=2), encoding="utf-8")
        return EXIT_FAILED
    dest = Path(args.dest)
    jobs = Path(config.jobs_dir)
    checks: list[tuple[str, bool, str]] = []  # (имя, ok, пояснение)

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    checks.append(("ffmpeg", bool(ffmpeg), ffmpeg or "не найден в PATH"))
    checks.append(("ffprobe", bool(ffprobe), ffprobe or "не найден в PATH"))

    try:
        jobs.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(jobs).free
        ok = free >= config.disk_budget_bytes
        checks.append(("место под задания", ok,
                       f"свободно {free} байт, бюджет "
                       f"{config.disk_budget_bytes} байт"))
    except OSError as exc:
        checks.append(("место под задания", False, str(exc)))

    url_state = "не задан — live недоступен, dry-run работает"
    if config.proxy_base_url:
        host = url_host(config.proxy_base_url)
        url_state = f"задан, схема/хост корректны (хост: {host})"
    checks.append(("адрес прокси", True, url_state))
    checks.append(("имя модели", True,
                   config.model or "не задано — live недоступен, "
                                   "dry-run работает"))
    checks.append(("ключ прокси", True,
                   "задан" if config.proxy_api_key else
                   "не задан — live недоступен (dry-run не нужен)"))
    checks.append(("замер памяти (/proc)",
                   Path("/proc/self/status").is_file(),
                   "доступен" if Path("/proc/self/status").is_file()
                   else "недоступен — в отчёте будет unavailable"))
    dest.mkdir(parents=True, exist_ok=True)

    print("=== doctor: проверка среды ===")
    fails = 0
    for name, ok, note in checks:
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}] {name}: {note}")
        if not ok:
            fails += 1
    live_ready = (bool(config.proxy_base_url) and bool(config.model)
                  and bool(config.proxy_api_key))
    live_state = ("ГОТОВ (настройки заданы; запуск — с двойным "
                  "подтверждением)" if live_ready and fails == 0
                  else "НЕ ГОТОВ")
    print(f"  dry-run: {'ГОТОВ' if fails == 0 else 'НЕ ГОТОВ'}")
    print(f"  live:    {live_state}")
    summary = {"checks": [dict(zip(("name", "ok", "note"), c))
                          for c in checks],
               "dry_run_ready": fails == 0, "live_ready": live_ready}
    (dest / "doctor.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return EXIT_OK if fails == 0 else EXIT_FAILED


def url_host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower()


# ── run ───────────────────────────────────────────────────────────────────
def cmd_run(args) -> int:
    try:
        config, env = load_config(args)
    except ConfigError as exc:
        print(f"настройки некорректны: {exc} (см. doctor)")
        return EXIT_FAILED
    fragment_seconds = getattr(
        args, "live_fragment_seconds", LIVE_FRAGMENT_DEFAULT_SECONDS)
    if not (LIVE_FRAGMENT_MIN_SECONDS <= fragment_seconds
            <= LIVE_FRAGMENT_MAX_SECONDS):
        print(f"--live-fragment-seconds: допустимо "
              f"{LIVE_FRAGMENT_MIN_SECONDS}..{LIVE_FRAGMENT_MAX_SECONDS} "
              f"секунд (получено {fragment_seconds})")
        return EXIT_USAGE
    live = resolve_live(args, config, env)
    mode = "live" if live else "dry-run"
    try:
        vocabulary = parse_vocabulary(
            getattr(args, "vocabulary", None),
            getattr(args, "vocabulary_file", None))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"словарь некорректен: {exc}")
        return EXIT_USAGE
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    # БД/задания — под dest, если не заданы явно; модель для dry-run
    env = dict(env)
    env.setdefault("SECRETARY_DB_PATH", str(dest / "db.sqlite"))
    env.setdefault("SECRETARY_JOBS_DIR", str(dest / "jobs"))
    if not live:
        env.setdefault("SECRETARY_MODEL", "mock-model")
    if getattr(args, "keep_segments", False):
        env["SECRETARY_KEEP_SEGMENT_FILES"] = "1"
    config = SecretaryConfig.from_env(env)

    t_start = time.monotonic()
    selection = resolve_selection(args, config)
    source_path = Path(selection["path"])
    source_sha256 = sha256_of(source_path)   # контрольная сумма источника
    source_size = source_path.stat().st_size
    t_select = time.monotonic() - t_start
    run_id = uuid.uuid4().hex[:12]
    owner = f"proxy-check-{run_id}"

    if live and len(selection["selected"]) > 1:
        print("live-прогон обрабатывает ОДНУ запись: укажите --select/--index "
              "(--all в live не применяется)")
        return EXIT_USAGE

    fragment_info: dict | None = None
    fragment_path: Path | None = None
    import_selection = selection
    t_fragment = 0.0
    if live:
        # в прокси уходит ТОЛЬКО локально вырезанный короткий фрагмент,
        # а не вся (возможно многочасовая) запись
        t_frag0 = time.monotonic()
        try:
            fragment_path = cut_live_fragment(
                selection, seconds=fragment_seconds, config=config,
                workdir=dest / "live-fragment")
        except (RuntimeError, ZipImportError) as exc:
            print(f"live-фрагмент не создан: {exc}")
            return EXIT_FAILED
        info = audio_mod.probe(fragment_path)
        fragment_info = {
            "requested_seconds": fragment_seconds,
            "offset_seconds": 0.0,
            "filename": fragment_path.name,
            "size_bytes": fragment_path.stat().st_size,
            "sha256": sha256_of(fragment_path),
            "duration_seconds": round(info.duration, 1),
        }
        t_fragment = time.monotonic() - t_frag0
        print(f"[live] локально вырезан фрагмент: запрошено "
              f"{fragment_seconds} с (фактически "
              f"{fragment_info['duration_seconds']} с, "
              f"{fragment_info['size_bytes']} байт) — в прокси уходит только "
              f"он, исходная запись целиком не отправляется")
        import_selection = {"type": "file", "path": str(fragment_path),
                            "selected": selection["selected"], "zip": None}

    if live:
        from secretary.transport import HttpTransport
        transport = _CountingTransport(HttpTransport(
            config.proxy_base_url, config.proxy_api_key,
            auth_header=config.auth_header,
            timeout=config.request_timeout_seconds,
            allow_loopback_http=config.allow_loopback_http))
    else:
        transport = _CountingTransport(build_dry_run_transport(env))
    engine = build_engine(config, transport)

    print(f"[{TOOL}] запуск {run_id}, режим {mode}, источник: "
          f"{source_path.name}")

    sampler = MemorySampler()
    sampler.start()
    t0 = time.monotonic()
    try:
        try:
            manifest = do_import(engine, import_selection, owner=owner,
                                 language=args.language,
                                 vocabulary=vocabulary, dest=dest)
        except (AudioError, ValidationError, ZipImportError) as exc:
            print(f"импорт не удался: {exc}")
            sampler.stop()
            engine.store.close()
            return EXIT_FAILED
    finally:
        # временный live-фрагмент не переживает прогон: у задания своя копия
        if fragment_path is not None:
            fragment_path.unlink(missing_ok=True)
    job_ids = list(dict.fromkeys(
        entry["job_id"] for entry in manifest["entries"]
        if entry.get("kind") == "audio"))
    t_import = time.monotonic() - t0

    if not job_ids:
        print("аудио-заданий не создано (см. manifest.json) — прогон "
              "невозможен")
        return EXIT_FAILED

    print(f"[1/3] импорт: заданий {len(job_ids)}, выбрано: "
          f"{', '.join(selection['selected'])}")
    t1 = time.monotonic()
    result = run_pipeline(engine, job_ids, sampler=sampler)
    t_transcribe = time.monotonic() - t1
    sampler.stop()
    print(f"[2/3] расшифровка: {result['status']} "
          f"({', '.join(f'{k} {v}' for k, v in result['counts'].items() if v and k != 'expected')})")

    t2 = time.monotonic()
    jobs = collect_jobs(engine, job_ids,
                        include_text=getattr(args, "include_transcript",
                                             False))
    environ = environment_report(config)
    source_info = {
        "filename": source_path.name,
        "selected": selection["selected"],
        "size_bytes": source_size,
        "sha256": source_sha256,
        "proxy_host": url_host(config.proxy_base_url)
        if live and config.proxy_base_url else None,
        "language": args.language,
        "vocabulary_terms": len(vocabulary),
    }
    if fragment_info is not None:
        source_info["live_fragment"] = fragment_info
    timings = {"select_seconds": t_select,
               "fragment_seconds": t_fragment,
               "import_seconds": t_import,
               "transcribe_seconds": t_transcribe,
               "network_seconds": transport.send_seconds,
               "report_seconds": 0.0,
               "total_seconds": time.monotonic() - t_start}
    report = build_report(run_id=run_id, mode=mode, source=source_info,
                          selection=selection, jobs=jobs,
                          transcription=result, timings=timings,
                          memory=sampler.report(),
                          transport_requests=transport.sent,
                          environment=environ,
                          include_transcript=getattr(
                              args, "include_transcript", False))
    report["timings"]["report_seconds"] = round(time.monotonic() - t2, 3)
    report["timings"]["total_seconds"] = round(
        report["timings"]["total_seconds"], 3)
    write_report(dest, report)
    write_csv(dest / "memory-samples.csv", sampler.samples)
    run_state = {"run_id": run_id, "mode": mode, "owner": owner,
                 "source": str(selection["path"]),
                 "selected": selection["selected"], "job_ids": job_ids,
                 "db_path": config.db_path, "jobs_dir": config.jobs_dir,
                 "manifest_path": manifest.get("manifest_path"),
                 "import_dir": str(dest / "imports"),
                 "source_sha256": source_sha256,
                 "source_size_bytes": source_size,
                 "language": args.language,
                 "vocabulary_terms": len(vocabulary),
                 "live_fragment": fragment_info,
                 "created_at": report["created_at"]}
    (dest / RUN_FILE).write_text(
        json.dumps(run_state, ensure_ascii=False, indent=2),
        encoding="utf-8")
    review = build_review_package(dest, engine, run_state, report)
    print(f"[3/3] отчёт: {dest / 'report.json'}; пакет материалов: {review}")
    secure_tree(Path(config.jobs_dir))
    _secure_file(Path(config.db_path))
    workflow = report["workflow_status"]
    stages = report["timings"]
    print(f"      этапы, с: выбор {stages['select_seconds']}, "
          f"фрагмент {stages['fragment_seconds']}, "
          f"импорт {stages['import_seconds']}, "
          f"расшифровка {stages['transcribe_seconds']} "
          f"(сеть {stages['network_seconds']}), "
          f"отчёт {stages['report_seconds']}, всего "
          f"{stages['total_seconds']}")
    print(f"      итог: {workflow}; память (пик): "
          f"{report['memory']['peak_rss_kb']} КиБ; запросов к транспорту: "
          f"{transport.sent}; AgroBot: публикация не выполнялась")
    engine.store.close()
    return EXIT_OK if workflow == "complete" else EXIT_FAILED


def _secure_file(path: Path) -> None:
    try:
        if path.is_file():
            path.chmod(0o600)
    except OSError:
        pass


# ── replay ────────────────────────────────────────────────────────────────
def cmd_replay(args) -> int:
    dest = Path(args.dest)
    run_file = dest / RUN_FILE
    if not run_file.is_file():
        print(f"последний запуск не найден: {run_file}")
        return EXIT_USAGE
    run_state = json.loads(run_file.read_text(encoding="utf-8"))
    config, env = load_config(args)
    live = resolve_live(args, config, env)
    mode = "live" if live else "dry-run"

    job_ids = run_state["job_ids"]  # строгая область ТОЛЬКО этого запуска
    print(f"[{TOOL}] replay запуска {run_state['run_id']} "
          f"(заданий {len(job_ids)}, режим {mode})")

    # БД/задачи — строго из состояния запуска (консистентность replay)
    env = dict(env)
    env["SECRETARY_DB_PATH"] = run_state["db_path"]
    env["SECRETARY_JOBS_DIR"] = run_state["jobs_dir"]
    if not live:
        env.setdefault("SECRETARY_MODEL", "mock-model")
    config = SecretaryConfig.from_env(env)

    if live:
        from secretary.transport import HttpTransport
        transport = _CountingTransport(HttpTransport(
            config.proxy_base_url, config.proxy_api_key,
            auth_header=config.auth_header,
            timeout=config.request_timeout_seconds,
            allow_loopback_http=config.allow_loopback_http))
    else:
        transport = _CountingTransport(build_dry_run_transport(env))
    engine = build_engine(config, transport)

    retried = 0
    if getattr(args, "retry_failed", False):
        for job_id in job_ids:
            job = engine.store.find_job(job_id)
            if job is None:
                continue
            if job["status"] in ("partial", "failed", "cancelled"):
                outcome = engine.retry_failed(job_id)
                if outcome.get("status") == "queued":
                    retried += 1
                    print(f"      перезапуск упавшего: {job_id[:8]} "
                          f"(сегментов в очередь: "
                          f"{outcome.get('pending', 0)})")

    sampler = MemorySampler()
    sampler.start()
    t0 = time.monotonic()
    result = run_pipeline(engine, job_ids, sampler=sampler)
    elapsed = time.monotonic() - t0
    sampler.stop()

    include_text = getattr(args, "include_transcript", False)
    t2 = time.monotonic()
    jobs = collect_jobs(engine, job_ids, include_text=include_text)
    selection = {"type": "replay", "selected": run_state["selected"]}
    source_info = {"filename": Path(run_state["source"]).name,
                   "selected": run_state["selected"],
                   "size_bytes": run_state.get("source_size_bytes"),
                   "sha256": run_state.get("source_sha256"),
                   "proxy_host": url_host(config.proxy_base_url)
                   if live and config.proxy_base_url else None}
    if run_state.get("live_fragment"):
        source_info["live_fragment"] = run_state["live_fragment"]
    report = build_report(run_id=run_state["run_id"], mode=mode,
                          source=source_info, selection=selection, jobs=jobs,
                          transcription=result,
                          timings={"select_seconds": 0.0,
                                   "fragment_seconds": 0.0,
                                   "import_seconds": 0.0,
                                   "transcribe_seconds": elapsed,
                                   "network_seconds": transport.send_seconds,
                                   "report_seconds": 0.0,
                                   "total_seconds": elapsed},
                          memory=sampler.report(),
                          transport_requests=transport.sent,
                          environment=environment_report(config),
                          replay={"retried_failed": retried,
                                  "processed": result["stats"].get(
                                      "processed", 0),
                                  "skipped_terminal": result["stats"].get(
                                      "skipped_terminal", 0),
                                  "original_mode": run_state["mode"]},
                          include_transcript=include_text)
    report["timings"]["report_seconds"] = round(time.monotonic() - t2, 3)
    report["timings"]["total_seconds"] = round(
        report["timings"]["total_seconds"], 3)
    write_report(dest, report)
    write_csv(dest / "memory-samples.csv", sampler.samples)
    review = build_review_package(dest, engine, run_state, report)
    print(f"      итог: {report['workflow_status']}; перезапущено упавших: "
          f"{retried}; запросов к транспорту: {transport.sent}")
    print(f"      отчёт: {dest / 'report.json'}; пакет: {review}")
    secure_tree(Path(config.jobs_dir))
    _secure_file(Path(config.db_path))
    engine.store.close()
    return EXIT_OK if report["workflow_status"] == "complete" else EXIT_FAILED


# ── review-package ────────────────────────────────────────────────────────
def cmd_review_package(args) -> int:
    dest = Path(args.dest)
    run_file = dest / RUN_FILE
    if not run_file.is_file():
        print(f"последний запуск не найден: {run_file}")
        return EXIT_USAGE
    run_state = json.loads(run_file.read_text(encoding="utf-8"))
    config, _ = load_config(args)
    config = SecretaryConfig.from_env({
        "SECRETARY_DB_PATH": run_state["db_path"],
        "SECRETARY_JOBS_DIR": run_state["jobs_dir"],
        "SECRETARY_MODEL": "mock-model"})
    engine = build_engine(config, _CountingTransport(
        build_dry_run_transport({})))
    review = build_review_package(dest, engine, run_state)
    print(f"пакет материалов пересобран: {review}")
    engine.store.close()
    return EXIT_OK


# ── CLI ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Проверка одной живой аудиозаписи через приватный "
                    "прокси: doctor / run (dry-run | live) / replay / "
                    "review-package. Публикация в AgroBot отсутствует "
                    "по построению.",
        epilog="Коды возврата: 0 успех; 1 ошибка выбора; 2 live заблокирован "
               "(нет двойного подтверждения/настроек); 3 неготовность среды "
               "или незавершённая расшифровка. Live: SECRETARY_LIVE_CONFIRM=1 "
               "+ --live-yes.")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p):
        p.add_argument("--env-file", default=None,
                       help="файл KEY=VALUE с настройками (права 0600; "
                            "ключи не в чате и не в репозитории)")

    doctor = sub.add_parser("doctor", help="проверка готовности среды")
    doctor.add_argument("--dest", default="data/proxy-check")
    _common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="прогон одной записи (dry-run по "
                                     "умолчанию, без сети)")
    run.add_argument("audio", help="аудиофайл или ZIP-архив")
    run.add_argument("--dest", default="data/proxy-check")
    run.add_argument("--select", default=None,
                     help="имя аудиозаписи в архиве (если их несколько)")
    run.add_argument("--index", type=int, default=None,
                     help="номер аудиозаписи в архиве (с 1)")
    run.add_argument("--all", action="store_true",
                     help="обработать все аудиозаписи архива")
    run.add_argument("--live", action="store_true",
                     help="живой прогон через приватный прокси (только с "
                          "двойным подтверждением)")
    run.add_argument("--live-yes", action="store_true",
                     help="явное подтверждение live-прогона (второе из двух)")
    run.add_argument("--live-fragment-seconds", type=int,
                     default=LIVE_FRAGMENT_DEFAULT_SECONDS, metavar="N",
                     help=f"live: длительность локально вырезаемого фрагмента,"
                          f" {LIVE_FRAGMENT_MIN_SECONDS}–"
                          f"{LIVE_FRAGMENT_MAX_SECONDS} с (по умолчанию "
                          f"{LIVE_FRAGMENT_DEFAULT_SECONDS}); в прокси "
                          f"отправляется ТОЛЬКО он")
    run.add_argument("--include-transcript", action="store_true",
                     help="включить полный текст расшифровки в отчёт "
                          "(отчёт становится закрытым: права 0600)")
    run.add_argument("--language", default=None)
    run.add_argument("--vocabulary", default=None,
                     help="термины предметной области через запятую")
    run.add_argument("--vocabulary-file", default=None,
                     help="UTF-8 файл: один термин на строку, # — комментарий")
    run.add_argument("--keep-segments", action="store_true",
                     help="диагностика: не удалять временные аудиофрагменты")
    _common(run)
    run.set_defaults(func=cmd_run)

    replay = sub.add_parser("replay", help="возобновление незавершённых "
                                           "заданий последнего запуска")
    replay.add_argument("--dest", default="data/proxy-check")
    replay.add_argument("--retry-failed", action="store_true",
                        help="явный перезапуск упавших/частичных заданий")
    replay.add_argument("--include-transcript", action="store_true",
                        help="включить полный текст расшифровки в отчёт "
                             "(отчёт закрывается правами 0600)")
    replay.add_argument("--live", action="store_true")
    replay.add_argument("--live-yes", action="store_true")
    _common(replay)
    replay.set_defaults(func=cmd_replay)

    review = sub.add_parser("review-package",
                            help="пересборка пакета материалов запуска")
    review.add_argument("--dest", default="data/proxy-check")
    _common(review)
    review.set_defaults(func=cmd_review_package)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
