import hashlib
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

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

    def test_load_clears_unused_sampling_defaults_for_greedy_decoding(self):
        # Stub modules stand in for torch, huggingface_hub and transformers: no download, no weights.
        generation = types.SimpleNamespace(do_sample=True, temperature=0.7, top_p=0.8, top_k=20,
                                           repetition_penalty=1.1)
        model = mock.Mock(generation_config=generation)
        model.to.return_value = model
        model.eval.return_value = model
        threads = []
        with tempfile.TemporaryDirectory() as path:
            Path(path, "model.safetensors").write_bytes(b"stub weights")
            stubs = {
                "torch": types.SimpleNamespace(set_num_threads=threads.append, float32="float32"),
                "huggingface_hub": types.SimpleNamespace(snapshot_download=lambda *args, **kwargs: path),
                "transformers": types.SimpleNamespace(
                    AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda *args, **kwargs: object()),
                    AutoModelForCausalLM=types.SimpleNamespace(from_pretrained=lambda *args, **kwargs: model)),
            }
            digest = mock.Mock(**{"hexdigest.return_value": WEIGHTS_SHA256})
            with mock.patch.dict(sys.modules, stubs), \
                    mock.patch.object(hashlib, "file_digest", return_value=digest):
                engine = Engine(start=False)
                engine._load()
        self.assertIsNone(engine.failure)
        self.assertTrue(engine.ready)
        self.assertEqual(threads, [2])
        model.to.assert_called_once_with("cpu")
        self.assertEqual((generation.temperature, generation.top_p, generation.top_k), (None, None, None))
        # Only the three sampling fields change; generate() still passes do_sample=False itself.
        self.assertEqual((generation.do_sample, generation.repetition_penalty), (True, 1.1))


if __name__ == "__main__":
    unittest.main()
