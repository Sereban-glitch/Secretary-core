"""Очередной, возобновляемый анализ видео через мультимодальную модель."""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from . import audio, video
from .config import SecretaryConfig
from .errors import (JobCancelled, PermanentTransportError,
                     TransientTransportError, ValidationError, VideoError)
from .prompts import VIDEO_ANALYZE_PROMPT, VIDEO_ANALYZE_PROMPT_VERSION
from .retry import RetryPolicy, retry_call
from .store import Store, canonical_video_params_hash
from .video_contract import build_video_request, extract_video_json

log = logging.getLogger("secretary.video_engine")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class VideoEngine:
    def __init__(self, config: SecretaryConfig, transport, store: Store, *,
                 prober=video.probe, extractor=video.extract_segment,
                 clock=time.monotonic, sleeper=time.sleep):
        self.config, self.transport, self.store = config, transport, store
        self.prober, self.extractor = prober, extractor
        self._clock, self._sleep = clock, sleeper
        self.jobs_dir = Path(config.jobs_dir)

    def create_job(self, video_path: Path | str, *, filename: str | None = None,
                   owner_id: str = "local", language: str | None = None,
                   vocabulary: list[str] | None = None,
                   move_source: bool = False,
                   source_url: str | None = None) -> dict:
        path = Path(video_path)
        if not path.is_file():
            raise ValidationError(f"file not found: {path}")
        size = path.stat().st_size
        if size > self.config.video_max_source_bytes:
            raise ValidationError("video exceeds configured source limit")
        mime = video.mime_for_file(path)
        info = self.prober(path)
        if info.duration > self.config.video_max_duration_seconds:
            raise ValidationError("video duration exceeds configured limit")
        terms = sorted({v.strip() for v in (vocabulary or []) if v.strip()})
        language = (language or "").strip() or None
        model = self.config.video_model or self.config.model
        if not model:
            raise ValidationError(
                "SECRETARY_VIDEO_MODEL or SECRETARY_MODEL is required")
        params_hash = canonical_video_params_hash(
            model=model, prompt_version=VIDEO_ANALYZE_PROMPT_VERSION,
            segment_seconds=self.config.video_segment_seconds,
            max_width=self.config.video_max_width,
            language=language, vocabulary=terms)
        ranges = video.segment_plan(
            info.duration, segment_seconds=self.config.video_segment_seconds,
            min_segment_seconds=self.config.video_min_segment_seconds)
        profile = {
            "model": model,
            "video_prompt_version": VIDEO_ANALYZE_PROMPT_VERSION,
            "segment_seconds": self.config.video_segment_seconds,
            "video_pipeline": f"MP4 H264/AAC max-width {self.config.video_max_width}",
            "transport": type(self.transport).__name__,
            "language": language,
            "vocabulary_terms": len(terms),
            "has_audio": bool(info.has_audio),
            "source_type": "youtube" if source_url else "file",
            "source_url": source_url,
        }
        ext = path.suffix.lower().lstrip(".")
        proposed = uuid.uuid4().hex
        registered = False
        job_dir = self.jobs_dir / proposed
        job_dir.mkdir(parents=True, exist_ok=True)
        staged = job_dir / f".source.{ext}.tmp"
        try:
            self._place_source(path, staged, move_source)
            job_id, created = self.store.create_job(
                job_id=proposed, filename=filename or path.name,
                source_path=f"source.{ext}", source_ext=ext,
                source_size=staged.stat().st_size, source_sha256=_sha256(staged),
                source_mime=mime, source_duration=info.duration,
                owner_id=owner_id, language=language, vocabulary=terms,
                params_hash=params_hash, profile=profile, segments=ranges,
                media_kind="video")
            registered = created
            if not created:
                staged.unlink(missing_ok=True)
                job_dir.rmdir()
                job = self.store.get_job(job_id)
                return {"job_id": job_id, "created": False,
                        "status": job["status"], "segments_total": len(ranges),
                        "duplicate_of": job_id, "media_kind": "video"}
            os.replace(staged, job_dir / f"source.{ext}")
        except Exception:
            staged.unlink(missing_ok=True)
            if registered:
                self.store.purge_job(proposed)
            try:
                job_dir.rmdir()
            except OSError:
                pass
            raise
        return {"job_id": job_id, "created": True, "status": "queued",
                "segments_total": len(ranges), "duplicate_of": None,
                "media_kind": "video"}

    @staticmethod
    def _place_source(path: Path, staged: Path, move: bool) -> None:
        if move:
            try:
                os.replace(path, staged)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
        shutil.copy2(path, staged)
        if move:
            path.unlink(missing_ok=True)

    def run_job(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return str(job["status"])
        if job.get("media_kind") != "video":
            raise ValidationError("not a video job")
        audio.check_disk_budget(self.jobs_dir, self.config.disk_budget_bytes)
        if self.store.is_cancelled(job_id):
            self.store.update_job(job_id, status="cancelled")
            return "cancelled"
        src = self.jobs_dir / job_id / job["source_path"]
        if not src.is_file():
            self.store.update_job(job_id, status="failed",
                                  error="source file missing on disk")
            return "failed"
        try:
            self._process(job_id, src)
        except JobCancelled:
            self.store.update_job(job_id, status="cancelled")
            return "cancelled"
        status = self._finalize(job_id)
        if status == "completed" and not self.config.video_keep_source:
            src.unlink(missing_ok=True)
            self.store.append_job_warning(
                job_id, "source_deleted_after_processing: исходное видео удалено")
        return status

    def _process(self, job_id: str, src: Path) -> None:
        seg_dir = self.jobs_dir / job_id / "video-segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        while True:
            pending = self.store.pending_segments(job_id)
            if not pending:
                return
            restart = False
            for seg in pending:
                if self.store.is_cancelled(job_id):
                    raise JobCancelled()
                if self._process_segment(job_id, src, seg_dir, seg) == "split":
                    restart = True
                    break
            if not restart:
                return

    def _process_segment(self, job_id: str, src: Path, seg_dir: Path,
                         seg: dict) -> str:
        idx = int(seg["idx"])
        self.store.update_segment(job_id, idx, status="running")
        path = seg_dir / f"seg_{idx:04d}.mp4"
        try:
            size = self.extractor(
                src, seg["start"], seg["end"], path,
                max_width=self.config.video_max_width,
                timeout=self.config.ffmpeg_timeout_seconds)
        except VideoError as exc:
            self.store.update_segment(job_id, idx, status="failed", permanent=1,
                                      error=str(exc))
            return "failed"
        if size > self.config.video_max_segment_bytes:
            duration = seg["end"] - seg["start"]
            if duration / 2 >= self.config.video_min_segment_seconds:
                mid = seg["start"] + duration / 2
                self.store.replace_segment_with_split(
                    job_id, idx, [(seg["start"], mid), (mid, seg["end"])])
                self.store.append_job_warning(
                    job_id, f"video segment {idx} split: {size} bytes")
                path.unlink(missing_ok=True)
                return "split"
            path.unlink(missing_ok=True)
            self.store.update_segment(job_id, idx, status="failed", permanent=1,
                                      error="video segment exceeds proxy limit")
            return "failed"
        payload = path.read_bytes()
        path.unlink(missing_ok=True)
        job = self.store.get_job(job_id)
        prompt = VIDEO_ANALYZE_PROMPT
        if job.get("language"):
            prompt += f" Основной язык речи: {job['language']}."
        terms = job.get("vocabulary_json") or []
        if terms:
            prompt += " Возможные термины: " + ", ".join(terms) + "."
        request = build_video_request(
            payload, "video/mp4", job["profile_json"]["model"], prompt,
            max_bytes=self.config.video_max_segment_bytes)
        attempts = {"n": 0}

        def send():
            attempts["n"] += 1
            return self.transport.send(request)

        policy = RetryPolicy(
            max_attempts=self.config.max_attempts,
            backoff_base=self.config.backoff_base_seconds,
            backoff_max=self.config.backoff_max_seconds,
            retry_after_cap=self.config.retry_after_cap_seconds,
            clock=self._clock, sleep=self._sleep)
        try:
            response = retry_call(send, policy,
                                  cancelled=lambda: self.store.is_cancelled(job_id))
            result = extract_video_json(response)
        except JobCancelled:
            self.store.update_segment(job_id, idx, status="pending")
            raise
        except TransientTransportError as exc:
            self.store.update_segment(job_id, idx, status="failed", permanent=0,
                                      attempts=attempts["n"], error=exc.detail)
            return "failed"
        except (PermanentTransportError, ValueError) as exc:
            detail = exc.detail if isinstance(exc, PermanentTransportError) else str(exc)
            self.store.update_segment(job_id, idx, status="failed", permanent=1,
                                      attempts=attempts["n"], error=detail)
            return "failed"
        self.store.update_segment(
            job_id, idx, status="done", attempts=attempts["n"],
            text=json.dumps(result, ensure_ascii=False), warnings_json="[]")
        return "done"

    def _finalize(self, job_id: str) -> str:
        segments = self.store.list_segments(job_id)
        done = [s for s in segments if s["status"] == "done"]
        failed = [s for s in segments if s["status"] == "failed"]
        if failed and done:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "completed"
        parsed = [(s, json.loads(s["text"])) for s in done if s.get("text")]
        transcript_parts = [
            "# Видеоанализ: речь, надписи и события требуют проверки человеком."
        ]
        statements, uncertain, topics, summaries = [], [], [], []
        for seg, item in parsed:
            marker = f"[SEG {seg['idx']}] {audio.format_ts(seg['start'])}–{audio.format_ts(seg['end'])}"
            summaries.append(item["summary"])
            block = [marker, "Речь:\n" + (item["speech"] or "(речь отсутствует)")]
            if item["visible_text"]:
                block.append("Видимый текст:\n- " + "\n- ".join(item["visible_text"]))
            if item["events"]:
                event_lines = []
                for event in item["events"]:
                    absolute = float(seg["start"]) + float(event["time"])
                    event_lines.append(f"- {audio.format_ts(absolute)} — {event['description']}")
                block.append("Визуальные события:\n" + "\n".join(event_lines))
                topics.append({"title": f"События {marker}",
                               "segments": [int(seg["idx"])]})
            transcript_parts.append("\n".join(block))
            for fact in item["facts"]:
                statements.append({
                    "text": f"[{fact['source']}] {fact['text']}",
                    "segments": [int(seg["idx"])], "confidence": "supported"})
            for issue in item["uncertain"]:
                uncertain.append({"issue": issue,
                                  "segments": [int(seg["idx"])], "quote": ""})
        draft = None
        if done:
            draft = {
                "summary": " ".join(summaries)[:2000],
                "topics": topics,
                "statements": statements,
                "questionable_places": uncertain,
                "speakers_note": "Речь извлечена из видеодорожки; имена не назначались автоматически.",
            }
        warnings = []
        if failed:
            warnings.append("failed_segments: " + ", ".join(str(s["idx"]) for s in failed))
        self.store.update_job(
            job_id, status=status, transcript="\n\n".join(transcript_parts),
            structured_json=(json.dumps(draft, ensure_ascii=False) if draft else None),
            warnings_json=json.dumps(warnings, ensure_ascii=False))
        return status
