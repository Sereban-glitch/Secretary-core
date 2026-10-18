"""Движок расшифровки: create → сегменты → транспорт → сборка → структура.

Инварианты:
- status=completed только при полном успешном покрытии сегментов;
- ошибка части сегментов → partial/failed, никогда completed;
- полный транскрипт хранится дословно (текст модели не переписывается);
- структурированный черновик — отдельный шаг и отдельное поле;
- публикация в базу НЕ выполняется: это задача клиента после проверки
  человеком (см. docs/INTEGRATION.md);
- границы [SEG n] — измеренные границы нарезки ffmpeg, не отметки модели.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from typing import Callable

from . import audio
from .config import SecretaryConfig
from .errors import (AudioError, JobCancelled, NotFoundError,
                     PermanentTransportError, TransientTransportError,
                     ValidationError)
from .prompts import (STRUCTURE_MERGE_PROMPT_VERSION,
                      STRUCTURE_PROMPT_VERSION, TRANSCRIBE_PROMPT,
                      TRANSCRIBE_PROMPT_VERSION)
from .proxy_contract import (build_request, build_structure_merge_request,
                             build_structure_request, extract_structure_json,
                             extract_text)
from .retry import RetryPolicy, retry_call
from .store import Store, canonical_params_hash
from .structure import validate_draft

log = logging.getLogger("secretary.engine")

_SEAM_DEDUP_MAX_LINES = 8
_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
_AUDIO_PIPELINE = "ffmpeg mono 16kHz Opus 24k OGG"


def _remove_dir_if_empty(path: Path) -> None:
    """Убирает пустую job-директорию (сбой до/без регистрации в БД)."""
    try:
        path.rmdir()
    except OSError:
        pass  # не пуста или уже удалена



def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _norm_line(line: str) -> str:
    return " ".join(line.split()).casefold().rstrip(" .!?,;:…")


def _dedup_seam(prev_text: str, next_text: str) -> tuple[str, int]:
    """Убирает дублированные строки на стыке перекрытия сегментов."""
    prev_lines = [l.strip() for l in prev_text.splitlines() if l.strip()]
    next_lines = [l.strip() for l in next_text.splitlines() if l.strip()]
    norm_prev = [_norm_line(l) for l in prev_lines]
    norm_next = [_norm_line(l) for l in next_lines]
    best = 0
    limit = min(_SEAM_DEDUP_MAX_LINES, len(prev_lines), len(next_lines))
    for k in range(1, limit + 1):
        if norm_prev[-k:] == norm_next[:k]:
            best = k
    if best:
        next_lines = next_lines[best:]
    return "\n".join(next_lines), best


def _text_warnings(text: str) -> list[str]:
    warnings: list[str] = []
    unclear = text.count("[неразборчиво]")
    if unclear:
        warnings.append(f"unclear_markers: {unclear}")
    if _TIMESTAMP_RE.search(text):
        warnings.append(
            "model_timestamps_in_text: временные отметки в тексте НЕ являются "
            "измеренными и не проверялись")
    return warnings


class TranscriptionEngine:
    def __init__(self, config: SecretaryConfig, transport, store: Store, *,
                 prober=audio.probe, extractor=audio.extract_segment,
                 clock=time.monotonic, sleeper=time.sleep):
        self.config = config
        self.transport = transport
        self.store = store
        self.prober = prober
        self.extractor = extractor
        self._clock = clock
        self._sleep = sleeper
        self.jobs_dir = Path(config.jobs_dir)

    # ------------------------------------------------------------------ create
    def create_job(self, audio_path: Path | str, *, filename: str | None = None,
                   owner_id: str = "local", language: str | None = None,
                   vocabulary: list[str] | None = None,
                   move_source: bool = False) -> dict:
        """Регистрирует задание. Идемпотентно: (owner, sha256, params).

        Порядок устойчив к сбоям (ревью pass2, P2 «crash-between»):
        1) сначала файл переносится в job-директорию (staging .tmp);
        2) только затем пишется строка SQLite;
        3) затем staging атомарно переименовывается в source.<ext>.
        Сбой на любом шаге не оставляет «сломанный дубликат»: строка БД
        без файла либо удаляется (компенсация), либо чинится ленивым
        восстановлением в run_job (см. _recover_staged_source).

        move_source=True — вызывающий владеет файлом (HTTP-загрузка):
        перенос без полной копии (os.replace, при EXDEV — copy+unlink).
        """
        path = Path(audio_path)
        if not path.is_file():
            raise ValidationError(f"file not found: {path}")
        if path.stat().st_size > self.config.max_source_bytes:
            raise ValidationError(
                f"file exceeds max_source_bytes ({path.stat().st_size} > "
                f"{self.config.max_source_bytes})")
        mime = audio.mime_for_file(path)  # расширение + сигнатура
        info = self.prober(path)  # действительный формат и длительность
        if info.duration > self.config.max_duration_seconds:
            raise ValidationError(
                f"duration {info.duration:.0f}s exceeds max "
                f"{self.config.max_duration_seconds:.0f}s")
        sha256 = _sha256_file(path)
        vocabulary = sorted({v.strip() for v in (vocabulary or []) if v.strip()})
        language = (language or "").strip() or None

        params_hash = canonical_params_hash(
            model=self.config.model,
            transcribe_prompt_version=TRANSCRIBE_PROMPT_VERSION,
            structure_prompt_version=STRUCTURE_PROMPT_VERSION,
            segment_seconds=self.config.segment_seconds,
            overlap_seconds=self.config.overlap_seconds,
            min_segment_seconds=self.config.min_segment_seconds,
            language=language, vocabulary=vocabulary)
        profile = {
            "model": self.config.model,
            "transcribe_prompt_version": TRANSCRIBE_PROMPT_VERSION,
            "structure_prompt_version": STRUCTURE_PROMPT_VERSION,
            "structure_merge_prompt_version": STRUCTURE_MERGE_PROMPT_VERSION,
            "segment_seconds": self.config.segment_seconds,
            "overlap_seconds": self.config.overlap_seconds,
            "min_segment_seconds": self.config.min_segment_seconds,
            "audio_pipeline": _AUDIO_PIPELINE,
            "transport": type(self.transport).__name__,
            "language": language,
            "vocabulary_terms": len(vocabulary),
        }
        ranges = audio.segment_plan(
            info.duration,
            segment_seconds=self.config.segment_seconds,
            overlap_seconds=self.config.overlap_seconds,
            min_segment_seconds=self.config.min_segment_seconds)
        ext = path.suffix.lower().lstrip(".")
        job_id = uuid.uuid4().hex
        job_dir = self.jobs_dir / job_id

        # 1) staging файла ДО записи в БД: сбой копирования не оставляет
        #    idempotency-запись без файла (повторная попытка создаст задачу заново)
        job_dir.mkdir(parents=True, exist_ok=True)
        staged = job_dir / f".source.{ext}.tmp"
        try:
            self._place_source(path, staged, move=move_source)
        except Exception:
            _remove_dir_if_empty(job_dir)
            raise

        # 2) строка SQLite; дубликат — staging убираем, возвращаем существующую
        try:
            job_id, created = self.store.create_job(
                job_id=job_id,
                filename=filename or path.name,
                source_path=f"source.{ext}",
                source_ext=ext, source_size=path.stat().st_size if not move_source
                else staged.stat().st_size,
                source_sha256=sha256, source_mime=mime,
                source_duration=info.duration, owner_id=owner_id,
                language=language, vocabulary=vocabulary,
                params_hash=params_hash, profile=profile,
                segments=[(r.start, r.end) for r in ranges])
        except Exception:
            staged.unlink(missing_ok=True)
            _remove_dir_if_empty(job_dir)
            raise
        if not created:
            staged.unlink(missing_ok=True)
            _remove_dir_if_empty(job_dir)
            log.info("job creation deduplicated -> existing %s", job_id)
            job = self.store.get_job(job_id)
            return {"job_id": job_id, "created": False, "status": job["status"],
                    "segments_total": len(ranges),
                    "duplicate_of": job_id}

        # 3) атомарная финализация; сбой — компенсация: строка БД удаляется,
        #    чтобы повторная попытка не наткнулась на «сломанный дубликат»
        final = job_dir / f"source.{ext}"
        try:
            os.replace(staged, final)
        except OSError:
            log.exception("job %s: staging rename failed, compensating", job_id)
            staged.unlink(missing_ok=True)
            self.store.purge_job(job_id)
            _remove_dir_if_empty(job_dir)
            raise
        log.info("job %s created: %s segments, %.1fs, sha256=%s",
                 job_id, len(ranges), info.duration, sha256[:12])
        job = self.store.get_job(job_id)
        return {"job_id": job_id, "created": created, "status": job["status"],
                "segments_total": len(ranges), "duplicate_of": None}

    @staticmethod
    def _place_source(path: Path, staged: Path, *, move: bool) -> None:
        """Перенос/копирование исходника в staging (одна точка для тестов)."""
        if move:
            try:
                os.replace(path, staged)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # разные ФС: копируем потоково и удаляем исходник
        shutil.copy2(path, staged)

    def _recover_staged_source(self, job: dict, job_dir: Path) -> Path | None:
        """Ленивое восстановление после сбоя «между БД и rename».

        Состояние: строка SQLite есть, source.<ext> нет, но остался
        .source.<ext>.tmp (крах процесса между шагами 2 и 3 create_job).
        Возвращает путь к восстановленному source или None.
        """
        ext = job["source_ext"]
        final = job_dir / job["source_path"]
        if final.is_file():
            return final
        candidates = [p for p in job_dir.glob(f".source.{ext}.tmp")
                      if p.is_file()] if ext else []
        if len(candidates) != 1:
            return None
        try:
            os.replace(candidates[0], final)
        except OSError:
            log.exception("job %s: staged source recovery failed", job["id"])
            return None
        log.warning("job %s: recovered staged source after interrupted "
                    "creation (crash-between DB and rename)", job["id"])
        return final

    # -------------------------------------------------------------------- run
    def run_pending(self, *, max_jobs: int | None = None,
                    job_ids: list[str] | None = None) -> dict:
        """Обрабатывает queued задачи последовательно (тяжёлый воркер).

        job_ids=None — вся очередь (CLI-воркер);
        job_ids=[...] — ТОЛЬКО перечисленные задания. Область действия
        нужна оркестратору одного импорта: в общей БД могут лежать
        чужие/старые задания, и без явного набора job_ids воркер
        обработал бы и их (ревью pass3, P1 «смешение импортов»).
        Терминальные задания пропускаются, отсутствующие — фиксируются
        в статистике, порядок обработки = порядок в списке.
        """
        processed = 0
        if job_ids is None:
            while max_jobs is None or processed < max_jobs:
                job = self.store.claim_next_job()
                if job is None:
                    break
                try:
                    self.run_job(job["id"])
                except Exception:  # noqa: BLE001 - воркер не должен падать
                    log.exception("job %s crashed worker", job["id"])
                    self.store.reset_running_segments(job["id"])
                    self.store.update_job(job["id"], status="queued")
                processed += 1
            return {"processed": processed, "scope": "queue"}
        skipped = 0
        missing = 0
        for job_id in job_ids:
            if max_jobs is not None and processed >= max_jobs:
                break
            job = self.store.find_job(job_id)
            if job is None:
                missing += 1
                log.warning("job %s: listed for processing but not found "
                            "in store", job_id)
                continue
            if job["status"] not in ("queued", "running"):
                skipped += 1
                continue
            try:
                self.run_job(job_id)
            except Exception:  # noqa: BLE001 - воркер не должен падать
                log.exception("job %s crashed worker", job_id)
                self.store.reset_running_segments(job_id)
                self.store.update_job(job_id, status="queued")
            processed += 1
        return {"processed": processed, "skipped_terminal": skipped,
                "missing": missing, "scope": "job_ids"}

    def run_job(self, job_id: str) -> str:
        """Прогон одного задания до терминального статуса."""
        job = self.store.get_job(job_id)
        if job["status"] not in ("queued", "running"):
            return str(job["status"])
        try:
            audio.check_disk_budget(self.jobs_dir, self.config.disk_budget_bytes)
        except AudioError as exc:
            # нехватка места — временная: возвращаем в очередь
            self.store.update_job(job_id, status="queued", error=str(exc))
            raise
        if self.store.is_cancelled(job_id):
            self.store.update_job(job_id, status="cancelled")
            return "cancelled"
        job_dir = self.jobs_dir / job_id
        src = job_dir / job["source_path"]
        if not src.is_file():
            # ленивое восстановление после краха между записью в БД и
            # финальным переименованием staging-файла (ревью pass2, P2)
            recovered = self._recover_staged_source(job, job_dir)
            if recovered is None:
                self.store.update_job(job_id, status="failed",
                                      error="source file missing on disk")
                log.error("job %s: source file missing", job_id)
                return "failed"
            src = recovered

        try:
            self._process_all_segments(job_id, src)
        except JobCancelled:
            self.store.update_job(job_id, status="cancelled")
            return "cancelled"
        return self._finalize(job_id)

    def _process_all_segments(self, job_id: str, src: Path) -> None:
        seg_dir = self.jobs_dir / job_id / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        while True:
            pending = self.store.pending_segments(job_id)
            if not pending:
                return
            for seg in pending:
                if self.store.is_cancelled(job_id):
                    raise JobCancelled()
                outcome = self._process_segment(job_id, src, seg_dir, seg)
                if outcome == "split":
                    break  # план изменился: перечитываем pending
            else:
                return

    def _process_segment(self, job_id: str, src: Path, seg_dir: Path,
                         seg: dict) -> str:
        idx = int(seg["idx"])
        self.store.update_segment(job_id, idx, status="running")
        seg_path = seg_dir / f"seg_{idx:04d}.ogg"
        try:
            size = self.extractor(src, seg["start"], seg["end"], seg_path,
                                  timeout=self.config.ffmpeg_timeout_seconds)
        except AudioError as exc:
            self.store.update_segment(job_id, idx, status="failed",
                                      permanent=1, error=str(exc))
            return "failed"
        if size > self.config.max_audio_bytes:
            duration = seg["end"] - seg["start"]
            if duration / 2 >= self.config.min_segment_seconds:
                mid = seg["start"] + duration / 2
                self.store.replace_segment_with_split(
                    job_id, idx, [(seg["start"], mid), (mid, seg["end"])])
                self.store.append_job_warning(
                    job_id,
                    f"segment {idx} split: exceeds proxy size limit "
                    f"({size} > {self.config.max_audio_bytes} bytes)")
                seg_path.unlink(missing_ok=True)
                return "split"
            self.store.update_segment(
                job_id, idx, status="failed", permanent=1,
                error=f"segment exceeds proxy size limit ({size} bytes) and "
                      f"cannot be split further")
            seg_path.unlink(missing_ok=True)
            return "failed"

        with open(seg_path, "rb") as handle:
            audio_bytes = handle.read()
        if not self.config.keep_segment_files:
            seg_path.unlink(missing_ok=True)

        prompt = self._transcribe_prompt(job_id)
        request = build_request(audio_bytes, "audio/ogg", self.config.model,
                                max_bytes=self.config.max_audio_bytes,
                                prompt=prompt)
        attempts = {"n": 0}

        def _send() -> dict:
            attempts["n"] += 1
            return self.transport.send(request)

        policy = RetryPolicy(max_attempts=self.config.max_attempts,
                             backoff_base=self.config.backoff_base_seconds,
                             backoff_max=self.config.backoff_max_seconds,
                             retry_after_cap=self.config.retry_after_cap_seconds,
                             clock=self._clock, sleep=self._sleep)
        try:
            response = retry_call(
                _send, policy,
                cancelled=lambda: self.store.is_cancelled(job_id),
                on_retry=lambda n, exc, delay: log.warning(
                    "job %s seg %d: attempt %d failed (%s), retry in %.1fs",
                    job_id, idx, n, exc.detail, delay))
        except JobCancelled:
            self.store.update_segment(job_id, idx, status="pending",
                                      error="cancelled during transport")
            raise
        except TransientTransportError as exc:
            self.store.update_segment(job_id, idx, status="failed",
                                      permanent=0,
                                      error=f"transient retries exhausted: {exc.detail}",
                                      attempts=attempts["n"])
            return "failed"
        except PermanentTransportError as exc:
            self.store.update_segment(job_id, idx, status="failed",
                                      permanent=1, error=exc.detail,
                                      attempts=attempts["n"])
            return "failed"
        try:
            text = extract_text(response)
        except ValueError as exc:
            # 200 с ошибкой/пустым ответом — не успех; квалификация: постоянная
            self.store.update_segment(job_id, idx, status="failed",
                                      permanent=1, error=f"invalid response: {exc}",
                                      attempts=attempts["n"])
            return "failed"
        warnings = _text_warnings(text)
        self.store.update_segment(job_id, idx, status="done", text=text,
                                  warnings_json=json.dumps(
                                      warnings, ensure_ascii=False),
                                  attempts=attempts["n"])
        return "done"

    def _transcribe_prompt(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        prompt = TRANSCRIBE_PROMPT
        if job.get("language"):
            prompt += f" Основной язык записи: {job['language']}."
        vocabulary = job.get("vocabulary_json") or []
        if vocabulary:
            prompt += (" Термины предметной области, которые могут "
                       f"встретиться: {', '.join(vocabulary)}.")
        return prompt

    # -------------------------------------------------------------- finalize
    def _finalize(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        segments = self.store.list_segments(job_id)
        done = [s for s in segments if s["status"] == "done"]
        failed = [s for s in segments if s["status"] == "failed"]
        cancelled = self.store.is_cancelled(job_id)
        if cancelled:
            status = "cancelled"
        elif failed and done:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "completed"

        transcript, warnings = self._assemble_transcript(segments)
        if cancelled and done:
            warnings.append(f"job_cancelled: расшифровано {len(done)} из "
                            f"{len(segments)} сегментов")
        if failed:
            warnings.append("failed_segments: " + ", ".join(
                str(s["idx"]) for s in failed))
        for s in done:
            for w in (s.get("warnings_json") or []):
                warnings.append(f"segment {s['idx']}: {w}")

        structured = None
        if done and status in ("completed", "partial"):
            structured, struct_warnings = self._structure_draft(job_id, segments)
            warnings.extend(struct_warnings)

        # ранее накопленные предупреждения (например, о split) не теряются
        existing = list(job.get("warnings_json") or [])
        merged = existing + [w for w in warnings if w not in existing]
        self.store.update_job(
            job_id, status=status, transcript=transcript,
            structured_json=(json.dumps(structured, ensure_ascii=False)
                             if structured is not None else None),
            warnings_json=json.dumps(merged, ensure_ascii=False))
        log.info("job %s finalized: %s (%d done, %d failed)",
                 job_id, status, len(done), len(failed))
        return status

    def _assemble_transcript(self, segments: list[dict]) -> tuple[str, list[str]]:
        parts: list[str] = ["# Границы вида [SEG n] ЧЧ:ММ:СС–ЧЧ:ММ:СС — "
                            "измеренные границы нарезки, не отметки модели."]
        warnings: list[str] = []
        prev_done: dict | None = None
        for s in segments:
            marker = (f"[SEG {s['idx']}] {audio.format_ts(s['start'])}–"
                      f"{audio.format_ts(s['end'])}")
            if s["status"] == "done":
                text = s["text"] or ""
                if prev_done is not None:
                    deduped, dropped = _dedup_seam(prev_done["text"] or "", text)
                    if dropped:
                        warnings.append(
                            f"overlap_dedup: стык {prev_done['idx']}/{s['idx']} — "
                            f"удалено {dropped} дублированных строк(и) перекрытия")
                        text = deduped
                parts.append(f"{marker}\n{text}")
                prev_done = s
            elif s["status"] == "failed":
                parts.append(f"{marker}\n[сегмент не расшифрован: {s['error']}]")
        return "\n\n".join(parts), warnings

    # -------------------------------------------------------------- structure
    def _structure_draft(self, job_id: str,
                         segments: list[dict]) -> tuple[dict | None, list[str]]:
        warnings: list[str] = []
        chunks = self._structure_chunks(segments)
        drafts: list[dict] = []
        for chunk in chunks:
            draft = self._structure_request(job_id, lambda repair,
                                             text=chunk: build_structure_request(
                                                 text, self.config.model,
                                                 max_chars=max(
                                                     self.config.structure_chunk_chars,
                                                     len(text)),
                                                 repair=repair))
            if draft is None:
                warnings.append("structuring_chunk_failed: не удалось "
                                "структурировать часть транскрипта")
            else:
                drafts.append(draft)
        if not drafts:
            return None, warnings + ["structuring_failed: структурированный "
                                     "черновик не создан (транскрипт сохранён)"]
        if len(drafts) == 1:
            return drafts[0], warnings
        merged = self._structure_merge(job_id, drafts)
        if merged is not None:
            return merged, warnings
        warnings.append("structuring_merge_failed: черновики частей не "
                        "объединены моделью; объединены механически")
        return _mechanical_merge(drafts), warnings

    def _structure_chunks(self, segments: list[dict]) -> list[str]:
        limit = self.config.structure_chunk_chars
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for s in segments:
            if s["status"] != "done":
                continue
            marker = (f"[SEG {s['idx']}] {audio.format_ts(s['start'])}–"
                      f"{audio.format_ts(s['end'])}\n")
            block = marker + (s["text"] or "")
            if current and current_len + len(block) > limit:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            current.append(block)
            current_len += len(block)
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    def _structure_request(self, job_id: str, request_builder) -> dict | None:
        """Отправляет запрос структурирования; одна попытка «ремонта» JSON.

        Транспортные ошибки уже прошли bounded retry внутри retry_call;
        вернуть None = отказ шага (транскрипт не затрагивается).
        """
        policy = RetryPolicy(max_attempts=self.config.max_attempts,
                             backoff_base=self.config.backoff_base_seconds,
                             backoff_max=self.config.backoff_max_seconds,
                             retry_after_cap=self.config.retry_after_cap_seconds,
                             clock=self._clock, sleep=self._sleep)
        for repair in (False, True):
            request = request_builder(repair)
            try:
                response = retry_call(
                    lambda: self.transport.send(request), policy,
                    cancelled=lambda: self.store.is_cancelled(job_id))
            except (TransientTransportError, PermanentTransportError) as exc:
                log.warning("job %s: structuring transport error: %s",
                            job_id, exc)
                return None
            except JobCancelled:
                raise
            try:
                return validate_draft(extract_structure_json(response))
            except ValueError as exc:
                log.warning("job %s: structuring parse error (repair=%s): %s",
                            job_id, repair, exc)
                continue
        return None

    def _structure_merge(self, job_id: str, drafts: list[dict]) -> dict | None:
        payload = json.dumps({"drafts": drafts}, ensure_ascii=False)
        if len(payload) > self.config.structure_chunk_chars:
            # механическое слияние вместо сверхкрупного запроса
            return None

        def _builder(repair: bool) -> dict:
            request = build_structure_merge_request(
                drafts, self.config.model,
                max_chars=max(self.config.structure_chunk_chars,
                              len(payload)))
            if repair:
                request = json.loads(json.dumps(request))
                request["messages"][0]["content"][0]["text"] += (
                    " Верни корректный JSON.")
            return request

        return self._structure_request(job_id, _builder)

    # ------------------------------------------------------------------ api
    def cancel(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            return str(job["status"])
        self.store.set_cancel_requested(job_id)
        if job["status"] == "queued":
            self.store.update_job(job_id, status="cancelled")
            return "cancelled"
        return "cancelling"

    def retry_failed(self, job_id: str) -> dict:
        """Явный перезапуск: failed → pending; отложенная отмена снимается.

        Отменённая задача с незавершёнными сегментами тоже возвращается
        в очередь — перезапуск продолжает незавершённые сегменты.

        Возврат после ПЕРЕЗАПУСКА процесса (pass5, регрессия возобновления):
        recover_stale_jobs() при открытии БД возвращает временно-упавшие
        сегменты (permanent=0) в pending, но задание остаётся failed —
        поэтому queued делается и при наличии незавершённых сегментов,
        а не только при свежеперепланированных failed.
        """
        job = self.store.get_job(job_id)
        if job["status"] not in ("partial", "failed", "cancelled"):
            return {"job_id": job_id, "requeued": 0, "pending": 0,
                    "status": job["status"]}
        requeued = self.store.requeue_failed_segments(job_id)
        pending = sum(1 for s in self.store.list_segments(job_id)
                      if s["status"] in ("pending", "running"))
        if requeued or pending:
            self.store.update_job(job_id, status="queued", cancel_requested=0,
                                  error=None)
        return {"job_id": job_id, "requeued": requeued, "pending": pending,
                "status": self.store.get_job(job_id)["status"]}

    def progress(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        segments = self.store.list_segments(job_id)
        counts = {"total": len(segments), "done": 0, "failed": 0, "pending": 0}
        for s in segments:
            key = s["status"] if s["status"] in ("done", "failed") else "pending"
            counts[key] += 1
        return {"job_id": job_id, "status": job["status"], "segments": counts,
                "created_at": job["created_at"], "updated_at": job["updated_at"]}

    def purge_job(self, job_id: str) -> None:
        self.store.get_job(job_id)  # NotFound, если нет
        shutil.rmtree(self.jobs_dir / job_id, ignore_errors=True)
        self.store.purge_job(job_id)

    def purge_finished_before(self, cutoff: float) -> list[str]:
        ids = self.store.purge_finished_before(cutoff)
        for job_id in ids:
            shutil.rmtree(self.jobs_dir / job_id, ignore_errors=True)
        return ids


def _mechanical_merge(drafts: list[dict]) -> dict:
    """Слияние без модели: конкатенация с дедупликацией тем."""
    summaries = [d["summary"] for d in drafts if d.get("summary")]
    topics: list[dict] = []
    seen_titles: set[str] = set()
    for d in drafts:
        for topic in d.get("topics", []):
            key = topic["title"].casefold()
            if key not in seen_titles:
                seen_titles.add(key)
                topics.append(topic)
    statements = [s for d in drafts for s in d.get("statements", [])]
    places = [p for d in drafts for p in d.get("questionable_places", [])]
    notes = [d.get("speakers_note", "") for d in drafts if d.get("speakers_note")]
    return validate_draft({
        "summary": " ".join(summaries)[:2000],
        "topics": topics,
        "statements": statements,
        "questionable_places": places,
        "speakers_note": " / ".join(notes)[:1000],
    })
