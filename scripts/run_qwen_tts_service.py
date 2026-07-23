"""Start the Qwen TTS service on loopback only."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import uvicorn

from backend.tts.contracts import SynthesisRequest
from backend.tts.qwen_service import QwenModelEngine, create_qwen_app


def validate_bind_host(host: str) -> str:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Qwen TTS service must bind to loopback")
    return host


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def main() -> None:
    host = validate_bind_host(os.environ.get("QWEN_TTS_HOST", "127.0.0.1"))
    port = int(os.environ.get("QWEN_TTS_PORT", "8090"))
    model_id = os.environ["QWEN_MODEL_ID"]
    model_file = Path(os.environ["QWEN_MODEL_CHECKSUM_FILE"]).resolve(strict=True)
    expected = os.environ["QWEN_MODEL_SHA256"]
    actual = sha256_file(model_file)
    if actual != expected:
        raise RuntimeError(f"Qwen model checksum mismatch: expected {expected}, got {actual}")
    warmup = SynthesisRequest.model_validate_json(
        Path(os.environ["QWEN_WARMUP_REQUEST_JSON"]).read_text(encoding="utf-8")
    )
    engine = QwenModelEngine(
        model_id=model_id,
        voice_root=Path(os.environ["QWEN_VOICE_ROOT"]),
        warmup_request=warmup,
        device=os.environ.get("QWEN_DEVICE"),
        dtype=os.environ.get("QWEN_DTYPE"),
    )
    uvicorn.run(create_qwen_app(engine, model_id, actual), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
