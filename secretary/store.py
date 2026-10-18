"""Постоянное хранилище задач: SQLite (stdlib), WAL, миграции.

Потокобезопасность: одно соединение + RLock; методы короткие, тяжёлая
работа (ffmpeg/сеть) выполняется вне блокировки.

Идемпотентность: UNIQUE(owner_id, source_sha256, params_hash) — повторное
создание с теми же параметрами возвращает существующую задачу.

Восстановление после перезапуска: running → queued; сегменты running →
pending; failed с временной причиной → pending; failed с постоянной
причиной остаются failed до явного retry-failed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .errors import NotFoundError

SCHEMA_VERSION = 2

_JOB_STATUS = ("queued", "running", "completed", "partial", "failed",
               "cancelled")
_SEGMENT_STATUS = ("pending", "running", "done", "failed")

_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        owner_id TEXT NOT NULL DEFAULT 'local',
        source_filename TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_ext TEXT NOT NULL,
        source_size INTEGER NOT NULL,
        source_sha256 TEXT NOT NULL,
        source_mime TEXT NOT NULL,
        source_duration REAL NOT NULL,
        language TEXT,
        vocabulary_json TEXT,
        params_hash TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        transcript TEXT,
        structured_json TEXT,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency
        ON jobs (owner_id, source_sha256, params_hash);
    CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at);

    CREATE TABLE IF NOT EXISTS segments (
        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        start REAL NOT NULL,
        end REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        permanent INTEGER NOT NULL DEFAULT 0,
        text TEXT,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (job_id, idx)
    );
    CREATE INDEX IF NOT EXISTS segments_job_status ON segments (job_id, status);
    """),
    (2, """
    ALTER TABLE jobs ADD COLUMN media_kind TEXT NOT NULL DEFAULT 'audio';
    CREATE INDEX IF NOT EXISTS jobs_kind_status_created
        ON jobs (media_kind, status, created_at);
    """),
]


class Store:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), timeout=30.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()
        self.recover_stale_jobs()

    # -- миграции/восстановление ------------------------------------------
    def migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            applied = {row[0] for row in
                       self._conn.execute("SELECT version FROM schema_version")}
            for version, sql in _MIGRATIONS:
                if version in applied:
                    continue
                with self._conn:
                    self._conn.executescript(sql)
                    self._conn.execute(
                        "INSERT INTO schema_version (version, applied_at) "
                        "VALUES (?, ?)", (version, time.time()))

    def recover_stale_jobs(self) -> dict:
        """Возврат незавершённого после перезапуска в очередь."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status='queued', updated_at=? "
                "WHERE status IN ('running')", (time.time(),))
            jobs_reset = cur.rowcount
            cur = self._conn.execute(
                "UPDATE segments SET status='pending', updated_at=? "
                "WHERE status='running'", (time.time(),))
            seg_reset = cur.rowcount
            cur = self._conn.execute(
                "UPDATE segments SET status='pending', attempts=0, "
                "updated_at=? WHERE status='failed' AND permanent=0",
                (time.time(),))
            seg_requeued = cur.rowcount
            # частичные задачи возвращаются в работу только явным retry-failed
        return {"jobs_reset": jobs_reset, "segments_reset": seg_reset,
                "transient_failed_requeued": seg_requeued}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- задания ------------------------------------------------------------
    def create_job(self, *, filename: str, source_path: str, source_ext: str,
                   source_size: int, source_sha256: str, source_mime: str,
                   source_duration: float, owner_id: str, language: str | None,
                   vocabulary: list[str], params_hash: str,
                   profile: dict, segments: list[tuple[float, float]],
                   media_kind: str = "audio",
                   job_id: str | None = None,
                   ) -> tuple[str, bool]:
        now = time.time()
        job_id = job_id or uuid.uuid4().hex
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE owner_id=? AND source_sha256=? "
                "AND params_hash=?", (owner_id, source_sha256, params_hash)
            ).fetchone()
            if row is not None:
                return str(row["id"]), False
            with self._conn:
                self._conn.execute(
                    "INSERT INTO jobs (id, created_at, updated_at, status, "
                    "owner_id, source_filename, source_path, source_ext, "
                    "source_size, source_sha256, source_mime, source_duration, "
                    "language, vocabulary_json, params_hash, profile_json, "
                    "media_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, now, now, "queued", owner_id, filename,
                     source_path, source_ext, source_size, source_sha256,
                     source_mime, source_duration, language,
                     json.dumps(vocabulary, ensure_ascii=False), params_hash,
                     json.dumps(profile, ensure_ascii=False), media_kind))
                self._conn.executemany(
                    "INSERT INTO segments (job_id, idx, start, end, status, "
                    "updated_at) VALUES (?,?,?,?,'pending',?)",
                    [(job_id, i, s, e, now) for i, (s, e) in enumerate(segments)])
        return job_id, True

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return _job_row_to_dict(row)

    def find_job(self, job_id: str) -> dict | None:
        try:
            return self.get_job(job_id)
        except NotFoundError:
            return None

    def list_jobs(self, *, status: str | None = None,
                  limit: int = 100) -> list[dict]:
        query = "SELECT * FROM jobs"
        params: list = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_job_row_to_dict(r) for r in rows]

    def claim_next_job(self) -> dict | None:
        """Атомарно забирает старейшую queued задачу в running."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE status='queued' "
                "ORDER BY created_at, id LIMIT 1").fetchone()
            if row is None:
                return None
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET status='running', updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (time.time(), row["id"]))
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return _job_row_to_dict(updated) if updated is not None else None

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        if "status" in fields and fields["status"] not in _JOB_STATUS:
            raise ValueError(f"bad job status {fields['status']!r}")
        columns = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [time.time(), job_id]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE jobs SET {columns}, updated_at=? WHERE id=?", values)

    def set_cancel_requested(self, job_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                (time.time(), job_id))

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?",
                (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def append_job_warning(self, job_id: str, warning: str) -> None:
        job = self.get_job(job_id)
        warnings = list(job.get("warnings_json") or [])
        if warning not in warnings:
            warnings.append(warning)
        self.update_job(job_id, warnings_json=json.dumps(
            warnings, ensure_ascii=False))

    def purge_job(self, job_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def purge_finished_before(self, cutoff: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE updated_at < ? AND status IN "
                "('completed','failed','cancelled')", (cutoff,)).fetchall()
        ids = [str(r["id"]) for r in rows]
        for job_id in ids:
            self.purge_job(job_id)
        return ids

    # -- сегменты -----------------------------------------------------------
    def list_segments(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE job_id=? ORDER BY idx",
                (job_id,)).fetchall()
        return [_segment_row_to_dict(r) for r in rows]

    def pending_segments(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE job_id=? AND status='pending' "
                "ORDER BY idx", (job_id,)).fetchall()
        return [_segment_row_to_dict(r) for r in rows]

    def update_segment(self, job_id: str, idx: int, **fields) -> None:
        if "status" in fields and fields["status"] not in _SEGMENT_STATUS:
            raise ValueError(f"bad segment status {fields['status']!r}")
        columns = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [time.time(), job_id, idx]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE segments SET {columns}, updated_at=? "
                "WHERE job_id=? AND idx=?", values)

    def segment_attempts(self, job_id: str, idx: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM segments WHERE job_id=? AND idx=?",
                (job_id, idx)).fetchone()
        return int(row["attempts"]) if row else 0

    def replace_segment_with_split(self, job_id: str, idx: int,
                                   halves: list[tuple[float, float]]) -> None:
        """Делит сегмент idx на части (сохраняя порядок: idx, idx+1, ...).

        Сдвиг делается в два шага через отрицательные индексы: прямое
        обновление idx+shift нарушает UNIQUE(job_id, idx) построчно.
        """
        if len(halves) < 2:
            raise ValueError("split requires at least two parts")
        shift = len(halves) - 1
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT start, end FROM segments WHERE job_id=? AND idx=?",
                (job_id, idx)).fetchone()
            if row is None:
                raise NotFoundError(f"segment {job_id}/{idx} not found")
            self._conn.execute(
                "UPDATE segments SET idx = -idx WHERE job_id=? AND idx > ?",
                (job_id, idx))
            self._conn.execute(
                "UPDATE segments SET idx = -idx + ? WHERE job_id=? AND idx < 0",
                (shift, job_id))
            self._conn.execute(
                "DELETE FROM segments WHERE job_id=? AND idx=?", (job_id, idx))
            now = time.time()
            self._conn.executemany(
                "INSERT INTO segments (job_id, idx, start, end, status, "
                "updated_at) VALUES (?,?,?,?,'pending',?)",
                [(job_id, idx + offset, start, end, now)
                 for offset, (start, end) in enumerate(halves)])

    def requeue_failed_segments(self, job_id: str) -> int:
        """Явный retry-failed: все failed возвращаются в pending."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE segments SET status='pending', permanent=0, attempts=0, "
                "error=NULL, updated_at=? WHERE job_id=? AND status='failed'",
                (time.time(), job_id))
            return cur.rowcount

    def reset_running_segments(self, job_id: str) -> int:
        """Аварийный сброс застрявших running-сегментов после падения воркера."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE segments SET status='pending', updated_at=? "
                "WHERE job_id=? AND status='running'",
                (time.time(), job_id))
            return cur.rowcount


def _job_row_to_dict(row: sqlite3.Row) -> dict:
    job = dict(row)
    for key in ("warnings_json", "vocabulary_json", "profile_json",
                "structured_json"):
        if job.get(key) is not None:
            job[key] = json.loads(job[key])
    return job


def _segment_row_to_dict(row: sqlite3.Row) -> dict:
    segment = dict(row)
    if segment.get("warnings_json") is not None:
        segment["warnings_json"] = json.loads(segment["warnings_json"])
    return segment


def canonical_params_hash(*, model: str, transcribe_prompt_version: str,
                          structure_prompt_version: str, segment_seconds: float,
                          overlap_seconds: float, min_segment_seconds: float,
                          language: str | None, vocabulary: list[str]) -> str:
    """Хеш параметров, влияющих на результат (не только хеш файла!)."""
    import hashlib
    payload = json.dumps({
        "model": model,
        "transcribe_prompt_version": transcribe_prompt_version,
        "structure_prompt_version": structure_prompt_version,
        "segment_seconds": segment_seconds,
        "overlap_seconds": overlap_seconds,
        "min_segment_seconds": min_segment_seconds,
        "language": language,
        "vocabulary": sorted(vocabulary),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_video_params_hash(*, model: str, prompt_version: str,
                                segment_seconds: float, max_width: int,
                                language: str | None,
                                vocabulary: list[str]) -> str:
    """Идемпотентность видео учитывает все параметры анализа."""
    import hashlib
    payload = json.dumps({
        "media_kind": "video",
        "model": model,
        "prompt_version": prompt_version,
        "segment_seconds": segment_seconds,
        "max_width": max_width,
        "language": language,
        "vocabulary": sorted(vocabulary),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
