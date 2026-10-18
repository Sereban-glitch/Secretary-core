import unittest

from secretary.errors import (JobCancelled, PermanentTransportError,
                              TransientTransportError)
from secretary.retry import RetryPolicy, compute_delay, retry_call


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_policy(**kwargs) -> tuple[RetryPolicy, FakeClock]:
    clock = FakeClock()
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("backoff_base", 1.0)
    kwargs.setdefault("backoff_max", 8.0)
    kwargs.setdefault("jitter", False)
    policy = RetryPolicy(clock=clock, sleep=clock.sleep, **kwargs)
    return policy, clock


class RetryTests(unittest.TestCase):
    def test_success_after_transient(self):
        policy, clock = make_policy()
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientTransportError("flaky")
            return {"ok": True}

        self.assertEqual(retry_call(fn, policy), {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertGreater(clock.now, 100.0)  # были паузы

    def test_exhausted_raises_last(self):
        policy, _ = make_policy(max_attempts=2)

        def fn():
            raise TransientTransportError("always down")

        with self.assertRaises(TransientTransportError):
            retry_call(fn, policy)

    def test_permanent_not_retried(self):
        policy, clock = make_policy()
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise PermanentTransportError("bad request")

        with self.assertRaises(PermanentTransportError):
            retry_call(fn, policy)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(clock.now, 100.0)  # без пауз

    def test_retry_after_honored_and_capped(self):
        policy, _ = make_policy(retry_after_cap=5.0)
        delay = compute_delay(policy, 1, retry_after=60.0)
        self.assertEqual(delay, 5.0)  # cap
        delay = compute_delay(policy, 1, retry_after=2.0)
        self.assertEqual(delay, 2.0)

    def test_backoff_growth_without_jitter(self):
        policy, _ = make_policy()
        self.assertEqual(compute_delay(policy, 1, None), 1.0)
        self.assertEqual(compute_delay(policy, 2, None), 2.0)
        self.assertEqual(compute_delay(policy, 3, None), 4.0)

    def test_jitter_bounds(self):
        policy, _ = make_policy(jitter=True, backoff_max=4.0)
        for attempt in (1, 2, 3, 5):
            delay = compute_delay(policy, attempt, None)
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, 4.0)

    def test_cancel_before_attempt(self):
        policy, _ = make_policy()
        with self.assertRaises(JobCancelled):
            retry_call(lambda: {"ok": 1}, policy, cancelled=lambda: True)

    def test_cancel_during_backoff(self):
        policy, clock = make_policy(max_attempts=5)
        state = {"cancelled": False}

        def fn():
            if not state["cancelled"]:
                state["cancelled"] = True  # отмена приходит в паузе
            raise TransientTransportError("flaky")

        def cancelled():
            return state["cancelled"]

        with self.assertRaises(JobCancelled):
            retry_call(fn, policy, cancelled=cancelled)

    def test_single_attempt(self):
        policy, _ = make_policy(max_attempts=1)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise TransientTransportError("one shot")

        with self.assertRaises(TransientTransportError):
            retry_call(fn, policy)
        self.assertEqual(calls["n"], 1)

    def test_on_retry_callback(self):
        policy, _ = make_policy()
        events = []
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientTransportError("first fails", retry_after=1.0)
            return {"ok": True}

        retry_call(fn, policy, on_retry=lambda n, exc, delay: events.append(
            (n, exc.detail, delay)))
        self.assertEqual(events, [(1, "first fails", 1.0)])


if __name__ == "__main__":
    unittest.main()
