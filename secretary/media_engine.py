"""Фасад одного последовательного воркера для аудио и видео."""
from __future__ import annotations

import logging

from .engine import TranscriptionEngine
from .video_engine import VideoEngine

log = logging.getLogger("secretary.media_engine")


class MediaEngine:
    def __init__(self, audio_engine: TranscriptionEngine,
                 video_engine: VideoEngine):
        self.audio = audio_engine
        self.video = video_engine
        self.config = audio_engine.config
        self.store = audio_engine.store
        self.jobs_dir = audio_engine.jobs_dir

    def create_job(self, *args, **kwargs):
        return self.audio.create_job(*args, **kwargs)

    def create_video_job(self, *args, **kwargs):
        return self.video.create_job(*args, **kwargs)

    def run_job(self, job_id: str) -> str:
        job = self.store.get_job(job_id)
        if job.get("media_kind") == "video":
            return self.video.run_job(job_id)
        return self.audio.run_job(job_id)

    def run_pending(self, *, max_jobs: int | None = None) -> dict:
        processed = 0
        while max_jobs is None or processed < max_jobs:
            job = self.store.claim_next_job()
            if job is None:
                break
            try:
                self.run_job(job["id"])
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed worker", job["id"])
                self.store.reset_running_segments(job["id"])
                self.store.update_job(job["id"], status="queued")
            processed += 1
        return {"processed": processed, "scope": "queue"}

    def progress(self, job_id: str):
        progress = self.audio.progress(job_id)
        progress["media_kind"] = (
            self.store.get_job(job_id).get("media_kind") or "audio")
        return progress

    def cancel(self, job_id: str):
        return self.audio.cancel(job_id)

    def retry_failed(self, job_id: str):
        return self.audio.retry_failed(job_id)

    def purge_job(self, job_id: str):
        return self.audio.purge_job(job_id)

    def purge_finished_before(self, cutoff: float):
        return self.audio.purge_finished_before(cutoff)
