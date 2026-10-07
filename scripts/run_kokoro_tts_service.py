"""Loopback Kokoro TTS service for short, song-grounded RadioTEDU links."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MODEL_NAME = "Kokoro-82M"
MODEL_REPO = "hexgrad/Kokoro-82M"
MODEL_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
MODEL_ROOT = Path(os.environ.get(
    "KOKORO_MODEL_PATH",
    r"C:\ProgramData\RadioTEDU\ai-radio\models\Kokoro-82M",
))
LANGUAGES = {"en": "a", "fr": "f"}
VOICE_PROFILES = {
    "en-day": ("am_michael", 1.04),
    "en-night": ("am_onyx", 0.98),
    "fr-day": ("ff_siwis", 1.02),
    "fr-night": ("ff_siwis", 0.97),
}
SAMPLE_RATE = 24_000
MODEL_WEIGHT_BYTES = 327_212_226


class KokoroEngine:
    """Load Kokoro language pipelines lazily and serialize CPU rendering."""

    def __init__(self) -> None:
        import torch

        torch.set_num_threads(max(1, min(2, int(os.environ.get("KOKORO_CPU_THREADS", "2")))))
        torch.set_num_interop_threads(1)
        self._torch = torch
        self._pipelines: dict[str, Any] = {}
        self._model: Any | None = None
        self._voices: dict[str, Any] = {}
        self._condition = threading.Condition()
        self._busy = False
        self._load_lock = threading.Lock()
        self._pipeline_error = ""
        self.last_error = ""
        self.last_generated_sha256 = ""
        self.last_rendered_chunks = 0
        self.cpu_threads = torch.get_num_threads()

    def _pipeline(self, language: str):
        if language not in LANGUAGES:
            raise ValueError("unsupported Kokoro output language")
        cached = self._pipelines.get(language)
        if cached is not None:
            return cached
        with self._load_lock:
            cached = self._pipelines.get(language)
            if cached is not None:
                return cached
            from kokoro import KModel, KPipeline

            if self._model is None:
                config_path = MODEL_ROOT / "config.json"
                weights_path = MODEL_ROOT / "kokoro-v1_0.pth"
                if (
                    not config_path.is_file()
                    or not weights_path.is_file()
                    or weights_path.stat().st_size != MODEL_WEIGHT_BYTES
                ):
                    raise RuntimeError("Kokoro model files are incomplete")
                self._model = KModel(
                    repo_id=MODEL_REPO,
                    config=str(config_path),
                    model=str(weights_path),
                ).to("cpu").eval()
            pipeline = KPipeline(
                lang_code=LANGUAGES[language],
                repo_id=MODEL_REPO,
                model=self._model,
                device="cpu",
            )
            self._pipelines[language] = pipeline
            self._pipeline_error = ""
            return pipeline

    def synthesize(
        self,
        text: str,
        language: str,
        profile_id: str,
        seed: int,
        is_cancelled=None,
    ) -> tuple[bytes, int]:
        if language not in LANGUAGES:
            raise ValueError("unsupported Kokoro output language")
        if profile_id not in VOICE_PROFILES or not profile_id.startswith(language + "-"):
            raise ValueError("unsupported Kokoro voice profile")
        with self._condition:
            while self._busy:
                if is_cancelled is not None and is_cancelled():
                    raise ConnectionAbortedError("speech client disconnected while queued")
                self._condition.wait(timeout=1 if is_cancelled is not None else None)
            if is_cancelled is not None and is_cancelled():
                raise ConnectionAbortedError("speech client disconnected before synthesis")
            self._busy = True
        try:
            import numpy as np
            import soundfile as sf

            pipeline = self._pipeline(language)
            voice_name, speed = VOICE_PROFILES[profile_id]
            voice = self._voices.get(voice_name)
            if voice is None:
                voice_path = MODEL_ROOT / "voices" / f"{voice_name}.pt"
                if not voice_path.is_file() or voice_path.stat().st_size < 100_000:
                    raise RuntimeError(f"Kokoro voice file is missing: {voice_name}")
                voice = self._torch.load(
                    voice_path, map_location="cpu", weights_only=True
                )
                self._voices[voice_name] = voice
            self._torch.manual_seed(int(seed) & 0x7FFF_FFFF)
            chunks: list[Any] = []
            with self._torch.inference_mode():
                for _graphemes, _phonemes, audio in pipeline(
                    text,
                    voice=voice,
                    speed=speed,
                    split_pattern=r"\n+",
                ):
                    waveform = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
                    waveform = np.asarray(waveform, dtype=np.float32).reshape(-1)
                    if waveform.size:
                        chunks.append(waveform)
            if not chunks:
                raise RuntimeError("Kokoro returned no speech audio")
            # Keep a small natural phrase boundary while preventing long pauses.
            gap = np.zeros(round(SAMPLE_RATE * 0.09), dtype=np.float32)
            pieces: list[Any] = []
            for index, chunk in enumerate(chunks):
                if index:
                    pieces.append(gap)
                pieces.append(chunk)
            waveform = np.concatenate(pieces)
            duration = len(waveform) / SAMPLE_RATE
            if not 1.0 <= duration <= 45.0:
                raise RuntimeError("Kokoro speech duration is outside the broadcast-safe range")
            output = io.BytesIO()
            sf.write(output, waveform, SAMPLE_RATE, format="WAV", subtype="PCM_16")
            payload = output.getvalue()
            if len(payload) <= 1024 or not (
                payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"
            ):
                raise RuntimeError("Kokoro generated an invalid WAV payload")
            self.last_error = ""
            self.last_generated_sha256 = hashlib.sha256(payload).hexdigest()
            self.last_rendered_chunks = len(chunks)
            return payload, len(chunks)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            raise
        finally:
            with self._condition:
                self._busy = False
                self._condition.notify_all()

    def health(self) -> dict[str, object]:
        return {
            "status": "ready",
            "engine": "kokoro",
            "model": MODEL_NAME,
            "model_repo": MODEL_REPO,
            "model_revision": MODEL_REVISION,
            "model_path": str(MODEL_ROOT),
            "languages_loaded": sorted(self._pipelines),
            "cpu_threads": self.cpu_threads,
            "fallback_tts": False,
            "last_error": self.last_error or self._pipeline_error,
            "last_generated_sha256": self.last_generated_sha256,
            "last_rendered_chunks": self.last_rendered_chunks,
            "queue": {"busy": self._busy},
        }


def create_server(host: str, port: int, engine: KokoroEngine) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Kokoro must bind to IPv4 loopback")

    class Handler(BaseHTTPRequestHandler):
        server_version = "RadioTEDU-Kokoro/1.0"

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return
            self._json(200, engine.health())

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/synthesize":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 2 or length > 4096:
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                text = str(request.get("text") or "").strip()
                if not text or len(text) > 800:
                    raise ValueError("invalid speech text")
                payload, rendered_chunks = engine.synthesize(
                    text=text,
                    language=str(request.get("language") or ""),
                    profile_id=str(request.get("design_id") or ""),
                    seed=int(request.get("seed") or 0),
                    is_cancelled=lambda: False,
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._json(503, {"error": f"Kokoro synthesis failed: {type(exc).__name__}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-TTS-Engine", "kokoro")
            self.send_header("X-TTS-Fallback", "false")
            self.send_header("X-TTS-Complete", "true")
            self.send_header("X-TTS-Hit-Ceiling", "false")
            self.send_header("X-TTS-Rendered-Chunks", str(rendered_chunks))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"kokoro-http {self.address_string()} {fmt % args}", flush=True)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    engine = KokoroEngine()
    server = create_server("127.0.0.1", int(os.environ.get("KOKORO_TTS_PORT", "8090")), engine)
    print(f"{MODEL_NAME} TTS ready on http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
