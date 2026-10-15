import threading
import unittest

from engine import Engine, MODEL_ID, MODEL_REVISION, WEIGHTS_SHA256


class EngineTests(unittest.TestCase):
    def test_disabled_is_not_simulated_readiness(self):
        engine = Engine(start=False)
        self.assertFalse(engine.ready)
        with self.assertRaisesRegex(RuntimeError, "model_unavailable"):
            engine.generate([{"role": "user", "content": "hello"}])
        self.assertEqual(engine.identity()["state"], "UNAVAILABLE")
        self.assertFalse(engine.identity()["external_actions"])
        self.assertFalse(engine.identity()["paid_provider_calls"])

    def test_identity_is_pinned_but_quality_not_claimed(self):
        self.assertEqual(MODEL_ID, "Qwen/Qwen2.5-0.5B-Instruct")
        self.assertEqual(len(MODEL_REVISION), 40)
        self.assertEqual(len(WEIGHTS_SHA256), 64)
        identity = Engine(start=False).identity()
        self.assertEqual(identity["quality_evaluation"], "UNKNOWN")
        self.assertEqual(identity["energy_joules"], "UNAVAILABLE")
        self.assertFalse(identity["weights_verified"])

    def test_rejects_caller_system_or_tool_authority(self):
        engine = Engine(start=False)
        engine.ready = True
        for role in ["system", "developer", "tool", "function"]:
            with self.subTest(role=role), self.assertRaises(ValueError):
                engine.generate([{"role": role, "content": "execute a command"}])

    def test_rejects_invalid_or_unbounded_messages(self):
        engine = Engine(start=False)
        engine.ready = True
        for messages in [None, [], [{}], [{"role": "user", "content": 42}],
                         [{"role": "user", "content": "x" * 8001}],
                         [{"role": "user", "content": "x"}] * 21]:
            with self.subTest(messages=str(messages)[:30]), self.assertRaises(ValueError):
                engine.generate(messages)

    def test_busy_model_does_not_start_second_generation(self):
        engine = Engine(start=False)
        engine.ready = True
        engine._lock = threading.Lock()
        engine._lock.acquire()
        with self.assertRaisesRegex(RuntimeError, "model_busy"):
            engine.generate([{"role": "user", "content": "hello"}])
        engine._lock.release()


if __name__ == "__main__":
    unittest.main()
