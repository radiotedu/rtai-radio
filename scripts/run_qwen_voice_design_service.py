"""Loopback-only Qwen radio-host TTS service with no alternate provider."""

from __future__ import annotations

import hashlib
import io
import json
import os
import gc
import select
import socket
import threading
import faulthandler
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


MODEL_NAME = "Qwen3-TTS-12Hz-0.6B-CustomVoice"
QUANTIZED_CACHE_VERSION = 1
# rtai-jingle lets Qwen stop on natural EOS. For an unattended radio service we
# also retain a finite 42-second runaway bound: 512 codec tokens at 12 Hz is
# safely beyond the validated 30-word copy limit. The wrapped generator records
# whether this ceiling was touched, and any such render is rejected outright.
MAX_NEW_TOKENS = 512
_PRONUNCIATION = (
    "Say the station name as two separate English words: 'Ted', rhyming with 'bed', "
    "followed by the pronoun 'you'. Never say 'tee-doo', 'tiidu', or spell individual letters."
)
_NO_SIGH = (
    " Start immediately with the first supplied word. Do not sigh, gasp, groan, hum, "
    "whisper, add audible breaths, or add any nonverbal sound. End cleanly after the last word."
)
SPEAKERS = {"en": "Ryan", "fr": "Serena"}
VOICE_DESIGNS = {
    # rtai-jingle-human-v5: probabilistic micro-variety baked into the instruct
    # itself —seed varies pitch/breath via manual_seed, but instruct must also
    # invite small human imperfections so successive renders never sound cloned.
    "en-day": (
        "Professional adult male English-language terrestrial music-radio presenter with a warm, mature timbre. "
        "Vocal warmth: rich and resonant. Energy: lively and balanced. Pace: concise natural radio "
        "pacing at about 1.2 to 1.5 words per second. "
        "Creative direction: friendly, conversational live FM host with a gentle smile, ordinary human breath "
        "groups, varied pitch and emphasis, and precise song titles. Use polished but spontaneous articulation. "
        "Never sound robotic, flat, childlike, whispered, sung, over-acted, distressed, tearful, sobbing, or crying. "
        "Never draw out vowels, words, or silences; keep breath pauses short. "
        "Finish the supplied copy cleanly and naturally, then stop. "
        + _PRONUNCIATION
        + _NO_SIGH
    ),
    "en-night": (
        "Professional adult male English-language late-night terrestrial music-radio presenter with a warm, mature close-mic "
        "timbre. Vocal warmth: deep and intimate. Energy: relaxed and controlled. Pace: concise natural "
        "radio pacing at about 1.2 to 1.5 words per second. Creative direction: calm one-to-one night host with a gentle smile, ordinary human breath "
        "groups, varied pitch, and precise song titles. Never whisper, sound robotic, flat, childlike, sung, "
        "over-acted, distressed, tearful, sobbing, or crying. Never draw out vowels, words, or silences; keep breath pauses short. "
        "Finish the supplied copy cleanly and naturally, then stop. "
        + _PRONUNCIATION
        + _NO_SIGH
    ),
    "fr-day": (
        "Professional native-French adult woman terrestrial music-radio presenter with a mature vocal timbre. "
        "Vocal warmth: rich and warmly resonant. Energy: lively and balanced. Pace: concise natural radio "
        "pacing at about 1.2 to 1.5 words per second. "
        "Creative direction: friendly conversational FM host with a gentle smile, ordinary human breath groups, "
        "varied pitch, natural liaison, and precise song titles. Never sound robotic, flat, childlike, whispered, "
        "sung, over-acted, distressed, tearful, sobbing, or crying. Never draw out vowels, words, or silences; "
        "keep breath pauses short. Finish the supplied copy cleanly and naturally, then stop. "
        + _PRONUNCIATION
        + _NO_SIGH
    ),
    "fr-night": (
        "Professional native-French adult woman late-night terrestrial music-radio presenter with a mature, "
        "velvety close-mic timbre. Vocal warmth: deeply warm and intimate. Energy: relaxed and controlled. "
        "Pace: concise natural radio pacing at about 1.2 to 1.5 words per second. Creative direction: calm one-to-one night host with a gentle smile, "
        "ordinary human breath groups, varied pitch, soft liaison, and precise song titles. Never whisper, sound "
        "robotic, flat, childlike, sung, over-acted, distressed, tearful, sobbing, or crying. Never draw out vowels, words, or silences; "
        "keep breath pauses short. Finish the supplied "
        "copy cleanly and naturally, then stop. "
        + _PRONUNCIATION
        + _NO_SIGH
    ),
}
LANGUAGES = {"en": "English", "fr": "French"}


@dataclass(frozen=True)
class SynthesisResult:
    payload: bytes
    generation_ceiling: int
    codec_tokens: int
    hit_ceiling: bool


def generated_codec_token_count(result: object) -> int:
    """Count Qwen time steps, not the 16 parallel codec codebooks."""
    try:
        codes = result[0]  # type: ignore[index]
        counts = [
            int(code.shape[0]) if hasattr(code, "shape") else len(code)
            for code in codes
        ]
        return max(counts, default=0)
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0


def generation_budget(text: str) -> int:
    """Give natural EOS ample room while bounding a distressed/runaway seed."""
    words = max(1, min(30, len(str(text).split())))
    # The waveform gate allows no slower than 0.9 words/sec plus three seconds.
    # Add another three-second codec margin and retain at least 24 seconds—four
    # times the unsafe old 72-token cap. Reaching this bound is rejected, never
    # published as truncated speech.
    guard_seconds = max(24.0, min(42.0, words / 0.9 + 6.0))
    return min(MAX_NEW_TOKENS, max(288, int(guard_seconds * 12 + 0.999)))


def configure_windows_resource_policy() -> str | None:
    """Keep Qwen CPU-bounded while retaining its model in physical memory."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    cpu_count = os.cpu_count() or 1
    raw_cores = os.environ.get("QWEN_TTS_CPU_CORES")
    if raw_cores is None:
        raw_cores = os.environ.get("QWEN_TTS_CPU_CORE", "-1")
    try:
        requested = [int(value.strip()) for value in raw_cores.split(",") if value.strip()]
    except ValueError as exc:
        raise RuntimeError("Qwen CPU affinity cores are invalid") from exc
    if not requested or requested == [-1]:
        cores = list(range(cpu_count))
    else:
        cores = sorted(set(requested))
        if any(core < 0 or core >= cpu_count for core in cores):
            raise RuntimeError("Qwen CPU affinity core is invalid")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetProcessInformation.restype = wintypes.BOOL
    kernel32.SetProcessWorkingSetSizeEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_size_t,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel32.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    affinity_mask = sum(1 << core for core in cores)
    if not kernel32.SetProcessAffinityMask(process, ctypes.c_size_t(affinity_mask)):
        raise OSError(ctypes.get_last_error(), "cannot set Qwen CPU affinity")

    class MemoryPriorityInformation(ctypes.Structure):
        _fields_ = [("MemoryPriority", wintypes.ULONG)]

    memory_priority_value = int(os.environ.get("QWEN_TTS_MEMORY_PRIORITY", "5"))
    if memory_priority_value not in {1, 2, 3, 4, 5}:
        raise RuntimeError("Qwen memory priority is invalid")
    memory_priority = MemoryPriorityInformation(memory_priority_value)
    if not kernel32.SetProcessInformation(
        process,
        0,  # ProcessMemoryPriority
        ctypes.byref(memory_priority),
        ctypes.sizeof(memory_priority),
    ):
        raise OSError(ctypes.get_last_error(), "cannot set Qwen memory priority")
    minimum_mb = int(os.environ.get("QWEN_TTS_MIN_WORKING_SET_MB", "2048"))
    maximum_mb = int(os.environ.get("QWEN_TTS_MAX_WORKING_SET_MB", "6144"))
    if minimum_mb < 512 or maximum_mb < minimum_mb:
        raise RuntimeError("Qwen working-set bounds are invalid")
    if not kernel32.SetProcessWorkingSetSizeEx(
        process,
        ctypes.c_size_t(minimum_mb * 1024 * 1024),
        ctypes.c_size_t(maximum_mb * 1024 * 1024),
        0x1,  # QUOTA_LIMITS_HARDWS_MIN_ENABLE
    ):
        raise OSError(ctypes.get_last_error(), "cannot retain Qwen working set")
    return ",".join(str(core) for core in cores)


class CustomVoiceEngine:
    """One CPU-limited Qwen CustomVoice model shared by all requests."""

    def __init__(self, model_path: Path) -> None:
        faulthandler.enable(all_threads=True)
        self.cpu_affinity_core = configure_windows_resource_policy()
        self.memory_priority = int(os.environ.get("QWEN_TTS_MEMORY_PRIORITY", "5"))
        self.minimum_working_set_mb = int(
            os.environ.get("QWEN_TTS_MIN_WORKING_SET_MB", "2048")
        )
        self.maximum_working_set_mb = int(
            os.environ.get("QWEN_TTS_MAX_WORKING_SET_MB", "6144")
        )
        model_path = model_path.resolve(strict=True)
        if not (model_path / "model.safetensors").is_file():
            raise RuntimeError("Qwen model weights are incomplete")
        model_config = json.loads((model_path / "config.json").read_text(encoding="utf-8"))
        self.model_kind = str(model_config.get("tts_model_type") or "").strip().casefold()
        if self.model_kind != "custom_voice":
            raise RuntimeError("radio host requires the Qwen CustomVoice checkpoint")
        self.model_name = model_path.name
        self.engine_name = "qwen-customvoice"
        try:
            print("Qwen CustomVoice: importing CPU runtime", flush=True)
            import torch
            from qwen_tts import Qwen3TTSModel
            from qwen_tts.inference.qwen3_tts_tokenizer import Qwen3TTSTokenizer
        except ImportError as exc:
            raise RuntimeError("Qwen TTS runtime dependencies are not installed") from exc
        # Leave CPU capacity for live audio encoding while keeping this small
        # CustomVoice model on the bounded local CPU inference path.
        requested_threads = int(os.environ.get("QWEN_TTS_CPU_THREADS", "1"))
        affinity_count = len(str(self.cpu_affinity_core or "").split(","))
        threads = max(1, min(3, requested_threads, affinity_count or 1))
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        # BF16 is the native low-memory checkpoint format on this CPU-only host.
        dtype_name = os.environ.get("QWEN_TTS_DTYPE", "bfloat16").strip().casefold()
        dtype = {"float32": torch.float32, "bfloat16": torch.bfloat16}.get(dtype_name)
        if dtype is None:
            raise RuntimeError("unsupported Qwen CPU dtype")
        quantization = os.environ.get("QWEN_TTS_QUANTIZATION", "none").strip().casefold()
        if quantization not in {"none", "dynamic-int8"}:
            raise RuntimeError("unsupported Qwen CPU quantization")
        self.dtype_name = dtype_name
        self.quantization = quantization
        self.max_new_tokens = MAX_NEW_TOKENS
        tokenizer_loader = Qwen3TTSTokenizer.from_pretrained.__func__

        def load_windows_tokenizer(cls, path: str, **kwargs):
            kwargs["use_safetensors"] = False
            return tokenizer_loader(cls, path, **kwargs)

        # Qwen's parent loader does not forward use_safetensors to its bundled
        # speech tokenizer, so select the identical converted tokenizer shards
        # explicitly on this Windows host.
        Qwen3TTSTokenizer.from_pretrained = classmethod(load_windows_tokenizer)
        print(
            f"Qwen radio host: loading {self.model_name} on CPU with {threads} threads as {dtype_name}",
            flush=True,
        )
        self._torch = torch
        # The service is inference-only.  Make that invariant explicit so
        # Transformers cannot retain autograd metadata during CPU speech renders.
        torch.set_grad_enabled(False)
        self.cpu_threads = threads
        self._model_path = model_path
        cache_root = Path(os.environ.get(
            "QWEN_TTS_CACHE_DIR",
            r"C:\ProgramData\RadioTEDU\ai-radio\models\optimized",
        ))
        cache_path = cache_root / f"{MODEL_NAME}-dynamic-int8-v{QUANTIZED_CACHE_VERSION}.pt"
        cache_meta_path = cache_path.with_suffix(".json")
        model_identity = hashlib.sha256(
            (model_path / "config.json").read_bytes()
            + (model_path / "pytorch_model.bin.index.json").read_bytes()
        ).hexdigest()
        self.quantization_source = "runtime"
        self._model = None
        if quantization == "dynamic-int8" and cache_path.is_file() and cache_meta_path.is_file():
            try:
                cache_meta = json.loads(cache_meta_path.read_text(encoding="utf-8"))
                if (
                    int(cache_meta.get("version") or 0) != QUANTIZED_CACHE_VERSION
                    or cache_meta.get("model") != MODEL_NAME
                    or cache_meta.get("model_identity") != model_identity
                ):
                    raise RuntimeError("stale Qwen optimized cache")
                print("Qwen radio host: loading verified dynamic-int8 cache", flush=True)
                cached_model = torch.load(
                    cache_path,
                    map_location="cpu",
                    weights_only=False,
                )
                quantized_linear = torch.ao.nn.quantized.dynamic.Linear
                if not any(isinstance(module, quantized_linear) for module in cached_model.model.modules()):
                    raise RuntimeError("optimized cache contains no dynamic-int8 layers")
                self._model = cached_model
                self.quantization_source = "verified-cache"
            except Exception as exc:
                print(f"Qwen radio host: ignoring unusable optimized cache: {exc}", flush=True)
                self._model = None
        if self._model is None:
            self._model = Qwen3TTSModel.from_pretrained(
                str(model_path),
                dtype=dtype,
                attn_implementation="sdpa",
                local_files_only=True,
                # The verified checkpoint is also serialized into small .bin
                # shards because Transformers' safetensors mmap slice faults in
                # torch.Storage.__getitem__ on this Windows host.  These shards
                # contain the identical Qwen tensors and avoid that native path.
                use_safetensors=False,
                # Load each legacy .bin shard directly into its destination tensor.
                # These shards are the same checkpoint tensors as the safetensors.
                low_cpu_mem_usage=True,
            )
        if quantization == "dynamic-int8":
            if self.quantization_source != "verified-cache":
                # Quantize only Linear weights in-place after the Qwen checkpoint
                # has loaded. This keeps its CustomVoice behavior; it only replaces eager
                # CPU matmuls with the built-in int8 dynamic kernels.
                print("Qwen radio host: applying eager dynamic-int8 linear weights", flush=True)
                self._model.model = torch.ao.quantization.quantize_dynamic(
                    self._model.model,
                    {torch.nn.Linear},
                    dtype=torch.qint8,
                    inplace=True,
                )
                gc.collect()
                cache_root.mkdir(parents=True, exist_ok=True)
                temporary_cache = cache_path.with_name(
                    f".{cache_path.name}.{os.getpid()}.tmp"
                )
                print("Qwen radio host: saving verified dynamic-int8 cache", flush=True)
                try:
                    # Qwen exposes this as a dict_keys view, which the standard
                    # torch serializer cannot pickle.  A list preserves the
                    # same public values and makes the optimized cache durable.
                    self._model.model.supported_speakers = list(
                        self._model.model.supported_speakers
                    )
                    torch.save(self._model, temporary_cache)
                    temporary_cache.replace(cache_path)
                    temporary_meta = cache_meta_path.with_name(
                        f".{cache_meta_path.name}.{os.getpid()}.tmp"
                    )
                    temporary_meta.write_text(json.dumps({
                        "version": QUANTIZED_CACHE_VERSION,
                        "model": MODEL_NAME,
                        "model_identity": model_identity,
                        "dtype": "float32",
                        "quantization": "dynamic-int8",
                        "tts_engine": "qwen-customvoice",
                        "fallback_tts": False,
                    }, indent=2), encoding="utf-8")
                    temporary_meta.replace(cache_meta_path)
                finally:
                    temporary_cache.unlink(missing_ok=True)
        self.last_codec_tokens = 0
        self.last_generation_hit_ceiling = False
        native_generate = self._model.model.generate

        def tracked_generate(*args, **kwargs):
            ceiling = int(kwargs.get("max_new_tokens") or MAX_NEW_TOKENS)
            self.last_codec_tokens = 0
            self.last_generation_hit_ceiling = False
            result = native_generate(*args, **kwargs)
            self.last_codec_tokens = generated_codec_token_count(result)
            self.last_generation_hit_ceiling = (
                self.last_codec_tokens <= 0 or self.last_codec_tokens >= ceiling
            )
            if self.last_codec_tokens <= 0:
                raise RuntimeError(
                    "Qwen CustomVoice did not report generated codec tokens"
                )
            if self.last_generation_hit_ceiling:
                raise RuntimeError(
                    "Qwen CustomVoice reached its codec ceiling before natural EOS"
                )
            return result

        self._model.model.generate = tracked_generate
        print("Qwen radio host: model load complete", flush=True)
        self._condition = threading.Condition()
        self._busy = False
        self._context_waiters = 0
        self._queue_waiters = 0
        self.last_error = ""
        self.last_generated_sha256 = ""

    @property
    def model_path(self) -> str:
        return str(self._model_path)

    def queue_snapshot(self) -> dict[str, int | bool]:
        with self._condition:
            return {
                "busy": self._busy,
                "context_waiters": self._context_waiters,
                "queue_waiters": self._queue_waiters,
            }

    def synthesize(
        self,
        text: str,
        language: str,
        design_id: str,
        seed: int,
        priority: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> SynthesisResult:
        if language not in LANGUAGES:
            raise ValueError("unsupported Qwen output language")
        if design_id not in VOICE_DESIGNS or not design_id.startswith(language + "-"):
            raise ValueError("unsupported Qwen voice design")
        if priority not in {"context", "queue"}:
            raise ValueError("unsupported Qwen queue priority")
        with self._condition:
            if priority == "context":
                self._context_waiters += 1
            else:
                self._queue_waiters += 1
            try:
                while self._busy or (priority == "queue" and self._context_waiters):
                    if is_cancelled is not None and is_cancelled():
                        raise ConnectionAbortedError("speech client disconnected while queued")
                    self._condition.wait(timeout=1 if is_cancelled is not None else None)
                if is_cancelled is not None and is_cancelled():
                    raise ConnectionAbortedError("speech client disconnected before synthesis")
                self._busy = True
            finally:
                if priority == "context":
                    self._context_waiters -= 1
                else:
                    self._queue_waiters -= 1
        try:
            self._torch.manual_seed(seed)
            try:
                generation = {
                    "text": text,
                    "language": LANGUAGES[language],
                    "speaker": SPEAKERS[language],
                    "instruct": VOICE_DESIGNS[design_id],
                    "max_new_tokens": generation_budget(text),
                    # Fixed speaker plus a concise style instruction; natural
                    # codec EOS is still required before the WAV is published.
                }
                with self._torch.inference_mode():
                    wavs, sample_rate = self._model.generate_custom_voice(**generation)
                if self.last_generation_hit_ceiling:
                    raise RuntimeError(
                        "Qwen CustomVoice reached its codec ceiling before natural EOS"
                    )
                if self.last_codec_tokens <= 0:
                    raise RuntimeError(
                        "Qwen CustomVoice did not report generated codec tokens"
                    )
                if not wavs:
                    raise RuntimeError("Qwen CustomVoice returned no waveform")
                import soundfile

                output = io.BytesIO()
                soundfile.write(
                    output, wavs[0], int(sample_rate), format="WAV", subtype="PCM_16"
                )
                payload = output.getvalue()
                if len(payload) <= 1024 or not (
                    payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"
                ):
                    raise RuntimeError("Qwen CustomVoice generated invalid WAV audio")
                self.last_error = ""
                self.last_generated_sha256 = hashlib.sha256(payload).hexdigest()
                return SynthesisResult(
                    payload=payload,
                    generation_ceiling=int(generation["max_new_tokens"]),
                    codec_tokens=self.last_codec_tokens,
                    hit_ceiling=self.last_generation_hit_ceiling,
                )
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:240]
                raise
        finally:
            with self._condition:
                self._busy = False
                self._condition.notify_all()


VoiceDesignEngine = CustomVoiceEngine  # compatibility for the existing service host


def create_server(host: str, port: int, engine: CustomVoiceEngine) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Qwen CustomVoice must bind to IPv4 loopback")

    class Handler(BaseHTTPRequestHandler):
        server_version = "RadioTEDU-Qwen-CustomVoice/1.0"

        def _peer_disconnected(self) -> bool:
            """Detect abandoned loopback requests before they consume the model."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

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
            self._json(200, {
                "status": "ready",
                "engine": getattr(engine, "engine_name", "qwen-customvoice"),
                "model": getattr(engine, "model_name", MODEL_NAME),
                "model_path": engine.model_path,
                "cpu_threads": getattr(engine, "cpu_threads", 1),
                "max_new_tokens": getattr(engine, "max_new_tokens", MAX_NEW_TOKENS),
                "cpu_affinity_core": getattr(engine, "cpu_affinity_core", None),
                "memory_priority": getattr(engine, "memory_priority", None),
                "minimum_working_set_mb": getattr(engine, "minimum_working_set_mb", None),
                "maximum_working_set_mb": getattr(engine, "maximum_working_set_mb", None),
                "dtype": getattr(engine, "dtype_name", "unknown"),
                "quantization": getattr(engine, "quantization", "none"),
                "quantization_source": getattr(engine, "quantization_source", "runtime"),
                "fallback_tts": False,
                "last_error": engine.last_error,
                "last_generated_sha256": engine.last_generated_sha256,
                "last_codec_tokens": getattr(engine, "last_codec_tokens", 0),
                "last_generation_hit_ceiling": getattr(
                    engine, "last_generation_hit_ceiling", False
                ),
                "queue": engine.queue_snapshot(),
            })

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
                result = engine.synthesize(
                    text=text,
                    language=str(request.get("language") or ""),
                    design_id=str(request.get("design_id") or ""),
                    seed=int(request.get("seed") or 0),
                    priority=str(request.get("priority") or "queue"),
                    is_cancelled=self._peer_disconnected,
                )
                if isinstance(result, SynthesisResult):
                    payload = result.payload
                    generation_ceiling = result.generation_ceiling
                    codec_tokens = result.codec_tokens
                    hit_ceiling = result.hit_ceiling
                else:
                    # Compatibility for lightweight test doubles only. The
                    # production CustomVoiceEngine always returns immutable
                    # per-request completion metadata.
                    payload = result
                    generation_ceiling = int(
                        getattr(engine, "max_new_tokens", MAX_NEW_TOKENS)
                    )
                    codec_tokens = int(getattr(engine, "last_codec_tokens", 0))
                    hit_ceiling = bool(
                        getattr(engine, "last_generation_hit_ceiling", True)
                    )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return
            except Exception:
                self._json(503, {"error": "Qwen CustomVoice synthesis failed"})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-TTS-Engine", getattr(engine, "engine_name", "qwen-customvoice"))
                self.send_header("X-TTS-Fallback", "false")
                self.send_header(
                    "X-TTS-Max-New-Tokens",
                    str(generation_ceiling),
                )
                self.send_header(
                    "X-TTS-Codec-Tokens",
                    str(codec_tokens),
                )
                self.send_header(
                    "X-TTS-Hit-Ceiling",
                    "true" if hit_ceiling else "false",
                )
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"qwen-http {self.address_string()} {fmt % args}", flush=True)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    model_path = Path(os.environ.get(
        "QWEN_MODEL_PATH",
        rf"C:\ProgramData\RadioTEDU\ai-radio\models\{MODEL_NAME}",
    ))
    engine = CustomVoiceEngine(model_path)
    server = create_server("127.0.0.1", int(os.environ.get("QWEN_TTS_PORT", "8090")), engine)
    print(f"{engine.model_name} ready on http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
