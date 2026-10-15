"""Bounded, text-only CPU inference. No tool or provider dispatch is available."""

import hashlib
import os
import threading
from contextlib import contextmanager

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
WEIGHTS_SHA256 = "fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe"
MAX_OUTPUT_TOKENS = 128
MAX_INPUT_TOKENS = 1024
SYSTEM = (
    "You are a text-only assistant. Give concise, useful answers. "
    "You cannot browse, execute tools, change accounts, or take external actions. "
    "Do not claim to have done so. Say when you lack evidence. "
    "Your answers are proposals for a person to review, not verified facts or authorization."
)


class Engine:
    def __init__(self, start=None):
        self.ready = False
        self.failure = None
        self._lock = threading.Lock()
        if start if start is not None else os.getenv("MODEL_ENABLED") == "1":
            threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoModelForCausalLM, AutoTokenizer

            path = snapshot_download(
                MODEL_ID, revision=MODEL_REVISION,
                allow_patterns=["*.json", "*.txt", "*.safetensors", "LICENSE"],
            )
            with open(os.path.join(path, "model.safetensors"), "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHTS_SHA256:
                    raise ValueError("weights_digest_mismatch")
            torch.set_num_threads(2)
            self._torch = torch
            self._tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
            self._model = AutoModelForCausalLM.from_pretrained(
                path, local_files_only=True, trust_remote_code=False,
                use_safetensors=True, dtype=torch.float32,
            ).to("cpu").eval()
            self.ready = True
        except Exception:
            self.failure = "model_load_failed"

    @contextmanager
    def admission(self, messages):
        if not self.ready:
            raise RuntimeError("model_unavailable")
        if not isinstance(messages, list) or not messages or len(messages) > 20:
            raise ValueError("invalid_messages")
        clean = [{"role": "system", "content": SYSTEM}]
        for item in messages:
            if (not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}
                    or not isinstance(item.get("content"), str)
                    or not 0 < len(item["content"]) <= 8000):
                raise ValueError("invalid_message")
            clean.append({"role": item["role"], "content": item["content"]})
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("model_busy")
        try:
            prompt = self._tokenizer.apply_chat_template(clean, tokenize=False, add_generation_prompt=True)
            inputs = self._tokenizer(prompt, return_tensors="pt")
            length = inputs["input_ids"].shape[-1]
            if length > MAX_INPUT_TOKENS:
                raise ValueError("context_limit")
            active = True
            used = False

            def generate():
                nonlocal used
                if not active or used:
                    raise RuntimeError("admission_consumed")
                used = True
                with self._torch.inference_mode():
                    result = self._model.generate(
                        **inputs, max_new_tokens=MAX_OUTPUT_TOKENS, do_sample=False,
                        max_time=45.0, pad_token_id=self._tokenizer.eos_token_id,
                    )
                tokens = result[0][length:]
                text = self._tokenizer.decode(tokens, skip_special_tokens=True).strip()
                if not text or len(text.encode("utf-8")) > 8192:
                    raise RuntimeError("invalid_model_output")
                return {"text": text, "output_tokens": len(tokens), "provider_response_id": None}

            try:
                yield generate
            finally:
                active = False
        finally:
            self._lock.release()

    def generate(self, messages):
        with self.admission(messages) as generate:
            return generate()

    def identity(self):
        return {
            "model": MODEL_ID, "revision": MODEL_REVISION,
            "weights_sha256": WEIGHTS_SHA256,
            "weights_verified": self.ready,
            "state": "MEASURED" if self.ready else "UNAVAILABLE",
            "quality_evaluation": "UNKNOWN", "energy_joules": "UNAVAILABLE",
            "external_actions": False, "paid_provider_calls": False,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "generation_timeout": "45 seconds best effort; not a hard deadline",
        }
