import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rook_ops import ConflictError, Store, ValidationError, money_to_micros


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")

    def tearDown(self): self.tmp.cleanup()

    def make(self, **extra):
        data = {"id": "T-1", "title": "Own outcome", "owner": "Rook", "priority": "high",
                "status": "in_progress", "next_action": "Run a verified step", "strategy": "A"}
        data.update(extra); return self.store.create_task(data)

    def test_persists_and_tracks_events(self):
        self.make(cost="1.25", credits_used="2.5")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_task("T-1")["cost_micros"], 1_250_000)
        self.assertEqual(reopened.events()[0]["type"], "task.created")

    def test_blocked_requires_blocker(self):
        with self.assertRaises(ValidationError): self.make(status="blocked")

    def test_approval_requires_detail(self):
        with self.assertRaises(ValidationError): self.make(status="approval")

    def test_done_requires_verification(self):
        self.make()
        with self.assertRaises(ValidationError): self.store.update_task("T-1", {"status": "done"})
        done = self.store.update_task("T-1", {"status": "done", "verification": "tests passed"})
        self.assertEqual(done["status"], "done")

    def test_circuit_breaker_and_strategy_change(self):
        self.make()
        for i in range(3):
            task = self.store.record_attempt("T-1", {"outcome": "failed", "strategy": "A", "error": f"fail {i}"})
        self.assertTrue(task["circuit_open"]); self.assertEqual(task["status"], "blocked")
        with self.assertRaises(ConflictError):
            self.store.record_attempt("T-1", {"outcome": "failed", "strategy": "A", "error": "again"})
        with self.assertRaises(ConflictError):
            self.store.update_task("T-1", {"status": "in_progress", "blocker": ""})
        changed = self.store.update_task("T-1", {"status": "in_progress", "strategy": "B", "hypothesis": "new evidence", "blocker": ""})
        self.assertFalse(changed["circuit_open"]); self.assertEqual(changed["failed_attempts"], 0)

    def test_attempt_cost_and_credit_accumulate_exactly(self):
        self.make()
        task = self.store.record_attempt("T-1", {"outcome": "succeeded", "strategy": "A", "cost": "0.123456", "credits_used": "1.000001"})
        self.assertEqual(task["cost_micros"], 123456); self.assertEqual(task["credits_used_micros"], 1000001)
        self.assertEqual(money_to_micros("0.000001"), 1)


if __name__ == "__main__": unittest.main()
