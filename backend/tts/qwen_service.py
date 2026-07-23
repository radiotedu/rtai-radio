"""Local, persistent Qwen TTS service with no alternate speech provider."""

from __future__ import annotations

import hashlib
import inspect
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Response

from .contracts import QwenHealth, SynthesisRequest


class QwenEngine(Protocol):
    """Minimal engine boundary used by the HTTP service and its tests."""

    def warmup(self) -> None: ...

    def synthesize(self, request: SynthesisRequest) -> tuple[bytes, int]: ...


class QwenModelEngine:
    """One loaded Qwen model, shared by all local service requests."""

    def __init__(
        self,
        model_id: str,
        voice_root: Path,
        warmup_request: SynthesisRequest,
        *,
        device: str | None = None,
        dtype: str | None = None,
    ) -> None:
        try:
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:  # pragma: no cover - requires broadcast hardware extras
            raise RuntimeError("Qwen TTS runtime dependencies are not installed") from exc

        load_options: dict[str, Any] = {}
        if device:
            if device != "cpu" and not device.startswith("cuda"):
                raise ValueError("Qwen device must be cpu or a cuda device")
            load_options["device_map"] = device
        if dtype:
            try:
                import torch
            except ImportError as exc:  # pragma: no cover - qwen-tts requires torch
                raise RuntimeError("PyTorch is required to select a Qwen dtype") from exc
            dtypes = {
                "float16": torch.float16,
                "float32": torch.float32,
                "bfloat16": torch.bfloat16,
            }
            if dtype not in dtypes:
                raise ValueError("Qwen dtype must be float16, float32, or bfloat16")
            load_options["dtype"] = dtypes[dtype]

        self._voice_root = voice_root.resolve(strict=True)
        self._warmup_request = warmup_request
        self._model = Qwen3TTSModel.from_pretrained(model_id, **load_options)

    def _approved_voice_file(self, relative_path: str) -> Path:
        candidate = (self._voice_root / relative_path).resolve(strict=True)
        try:
            candidate.relative_to(self._voice_root)
        except ValueError as exc:
            raise ValueError("Qwen voice asset must remain beneath QWEN_VOICE_ROOT") from exc
        return candidate

    def warmup(self) -> None:
        self.synthesize(self._warmup_request)

    def synthesize(self, request: SynthesisRequest) -> tuple[bytes, int]:
        """Generate a mono PCM-16 WAV using only an approved station voice asset."""
        clone_prompt = self._approved_voice_file(request.voice.clone_prompt_path)
        reference_audio = self._approved_voice_file(request.voice.reference_audio_path)
        method = self._model.generate_voice_clone
        parameters = inspect.signature(method).parameters
        qwen_language = {"en": "English", "fr": "French"}[request.language]
        arguments: dict[str, Any] = {
            "text": request.normalized_text,
            "language": qwen_language,
        }
        # Qwen releases have used different names for these optional inputs.  Pass
        # each only when its installed runtime supports it, while never accepting
        # a caller-supplied style or an unapproved asset path.
        optional_arguments = {
            "ref_audio": str(reference_audio),
            "ref_text": request.voice.reference_transcript,
            "instruct": request.voice.style_id,
            "style": request.voice.style_id,
            "clone_prompt_path": str(clone_prompt),
        }
        arguments.update({name: value for name, value in optional_arguments.items() if name in parameters})
        generated = method(**arguments)
        sample_rate = int(getattr(self._model, "sample_rate", 24000))
        if isinstance(generated, tuple):
            waveform, returned_sample_rate = generated
            sample_rate = int(returned_sample_rate)
        else:
            waveform = generated
        if isinstance(waveform, (list, tuple)):
            if len(waveform) != 1:
                raise RuntimeError(f"Qwen returned {len(waveform)} waveforms for one request")
            waveform = waveform[0]

        try:
            import soundfile
        except ImportError as exc:  # pragma: no cover - requires broadcast hardware extras
            raise RuntimeError("soundfile is required by the Qwen TTS runtime") from exc

        target = io.BytesIO()
        soundfile.write(target, waveform, sample_rate, format="WAV", subtype="PCM_16")
        return target.getvalue(), sample_rate


def create_qwen_app(engine: QwenEngine, model_id: str, model_checksum: str) -> FastAPI:
    """Create the loopback service API around one already-constructed engine."""
    state: dict[str, object] = {"warmed": False, "last_error": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        try:
            engine.warmup()
            state["warmed"] = True
        except Exception as exc:
            state["last_error"] = f"{type(exc).__name__}: {exc}"
        yield

    app = FastAPI(title="RadioTEDU Qwen TTS", lifespan=lifespan)

    @app.get("/health", response_model=QwenHealth)
    def health() -> QwenHealth:
        return QwenHealth(
            status="ready" if state["warmed"] else "unhealthy",
            warmed=bool(state["warmed"]),
            model_id=model_id,
            model_checksum=model_checksum,
            last_error=state["last_error"],
        )

    @app.post("/v1/synthesize")
    def synthesize(request: SynthesisRequest) -> Response:
        if not state["warmed"]:
            raise HTTPException(status_code=503, detail="Qwen model is not warmed")
        if request.voice.model_checksum != model_checksum:
            raise HTTPException(status_code=409, detail="request model checksum does not match loaded model")
        try:
            payload, sample_rate = engine.synthesize(request)
        except Exception as exc:
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            raise HTTPException(status_code=503, detail="Qwen synthesis failed") from exc
        if not payload.startswith(b"RIFF") or payload[8:12] != b"WAVE":
            raise HTTPException(status_code=502, detail="Qwen engine returned invalid WAV")
        digest = hashlib.sha256(payload).hexdigest()
        return Response(
            payload,
            media_type="audio/wav",
            headers={
                "X-Request-ID": request.request_id,
                "X-Model-Checksum": model_checksum,
                "X-Sample-Rate-Hz": str(sample_rate),
                "X-Audio-SHA256": digest,
            },
        )

    return app
