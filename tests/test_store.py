import tempfile
import time
import unittest
from pathlib import Path

from secretary.store import Store, canonical_params_hash


def make_store(tmp: str) -> Store:
    return Store(str(Path(tmp) / "db.sqlite"))


def create_job(store: Store, job_id: str, sha: str = "aa" * 32,
               owner: str = "local", params: str = "p1") -> str:
    store.create_job(
        filename="voice.ogg", source_path="source.ogg", source_ext="ogg",
        source_size=100, source_sha256=sha, source_mime="audio/ogg",
        source_duration=65.0, owner_id=owner, language=None, vocabulary=[],
        params_hash=params, profile={"model": "m"},
        segments=[(0.0, 30.0), (26.0, 65.0)], job_id=job_id)
    return job_id


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_migrations_idempotent(self):
        store2 = make_store(self._tmp.name)  # повторное открытие
        self.assertEqual(store2.recover_stale_jobs(), {
            "jobs_reset": 0, "segments_reset": 0,
            "transient_failed_requeued": 0})

    def test_idempotency(self):
        first, created = create_job(self.store, "job1"), True
        _, created2 = self.store.create_job(
            filename="voice.ogg", source_path="source.ogg",
            source_ext="ogg", source_size=100, source_sha256="aa" * 32,
            source_mime="audio/ogg", source_duration=65.0, owner_id="local",
            language=None, vocabulary=[], params_hash="p1",
            profile={"model": "m"}, segments=[(0.0, 30.0)])
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_claim_next_order(self):
        create_job(self.store, "job-a")
        time.sleep(0.01)
        create_job(self.store, "job-b", sha="bb" * 32)
        claimed = self.store.claim_next_job()
        self.assertEqual(claimed["id"], "job-a")
        claimed = self.store.claim_next_job()
        self.assertEqual(claimed["id"], "job-b")
        self.assertIsNone(self.store.claim_next_job())

    def test_cancel_flag(self):
        create_job(self.store, "job1")
        self.assertFalse(self.store.is_cancelled("job1"))
        self.store.set_cancel_requested("job1")
        self.assertTrue(self.store.is_cancelled("job1"))

    def test_split_shifts_indices(self):
        create_job(self.store, "job1")
        self.store.replace_segment_with_split("job1", 0,
                                              [(0.0, 15.0), (15.0, 30.0)])
        segments = self.store.list_segments("job1")
        self.assertEqual([(s["idx"], s["start"], s["end"]) for s in segments],
                         [(0, 0.0, 15.0), (1, 15.0, 30.0), (2, 26.0, 65.0)])
        # статусы нового сегмента — pending
        self.assertEqual(segments[0]["status"], "pending")

    def test_recover_stale(self):
        create_job(self.store, "job1")
        self.store.update_job("job1", status="running")
        self.store.update_segment("job1", 0, status="running")
        self.store.update_segment("job1", 1, status="failed", permanent=0,
                                  error="transient")
        result = self.store.recover_stale_jobs()
        self.assertEqual(result["jobs_reset"], 1)
        self.assertEqual(result["segments_reset"], 1)
        self.assertEqual(result["transient_failed_requeued"], 1)
        self.assertEqual(self.store.get_job("job1")["status"], "queued")
        statuses = [s["status"] for s in self.store.list_segments("job1")]
        self.assertEqual(statuses, ["pending", "pending"])

    def test_permanent_failure_not_requeued_on_recovery(self):
        create_job(self.store, "job1")
        self.store.update_job("job1", status="partial")
        self.store.update_segment("job1", 1, status="failed", permanent=1)
        result = self.store.recover_stale_jobs()
        self.assertEqual(result["transient_failed_requeued"], 0)
        segment = self.store.list_segments("job1")[1]
        self.assertEqual(segment["status"], "failed")

    def test_requeue_failed_segments(self):
        create_job(self.store, "job1")
        self.store.update_job("job1", status="partial")
        self.store.update_segment("job1", 1, status="failed", permanent=1,
                                  error="boom")
        self.assertEqual(self.store.requeue_failed_segments("job1"), 1)
        segment = self.store.list_segments("job1")[1]
        self.assertEqual(segment["status"], "pending")
        self.assertIsNone(segment["error"])

    def test_purge_only_finished(self):
        create_job(self.store, "job1")
        create_job(self.store, "job2", sha="cc" * 32)
        self.store.update_job("job1", status="completed")
        self.store.update_job("job2", status="running")
        # «давно завершённая» job1
        self.store._conn.execute(
            "UPDATE jobs SET updated_at=? WHERE id='job1'", (time.time() - 100,))
        self.store._conn.commit()
        purged = self.store.purge_finished_before(time.time() - 50)
        self.assertEqual(purged, ["job1"])
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_params_hash(self):
        base = dict(model="m", transcribe_prompt_version="v1",
                    structure_prompt_version="v1", segment_seconds=900.0,
                    overlap_seconds=4.0, min_segment_seconds=30.0,
                    language=None, vocabulary=[])
        same = canonical_params_hash(**base)
        self.assertEqual(same, canonical_params_hash(**base))
        # порядок словаря не влияет
        self.assertEqual(same, canonical_params_hash(
            model="m", vocabulary=[], language=None,
            min_segment_seconds=30.0, overlap_seconds=4.0,
            segment_seconds=900.0, structure_prompt_version="v1",
            transcribe_prompt_version="v1"))
        changed = dict(base, vocabulary=["Berthoud"])
        self.assertNotEqual(same, canonical_params_hash(**changed))
        changed = dict(base, model="m2")
        self.assertNotEqual(same, canonical_params_hash(**changed))

    def test_job_not_found(self):
        from secretary.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            self.store.get_job("missing")


if __name__ == "__main__":
    unittest.main()
