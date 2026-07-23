import hashlib
import io
import wave

import numpy as np
from fastapi.testclient import TestClient

from backend.tts.contracts import SynthesisRequest, VoiceSelection
from backend.tts.qwen_service import QwenModelEngine, create_qwen_app


def wav_bytes() -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00" * 2400)
    return target.getvalue()


def request() -> SynthesisRequest:
    return SynthesisRequest(
        request_id="req-service-1",
        station_id="radiotedu-en",
        language="en",
        locale="en-US",
        normalized_text="You are listening to RadioTEDU.",
        announcement_label="station_id",
        voice=VoiceSelection(
            station_id="radiotedu-en",
            language="en",
            locale="en-US",
            voice_pack="radiotedu-en-voices-v1",
            host_id="maya",
            style_id="energetic_clear",
            clone_prompt_path="maya.pt",
            reference_audio_path="maya.wav",
            reference_transcript="You are listening to RadioTEDU.",
            model_checksum="sha256:" + "a" * 64,
        ),
    )


class FakeEngine:
    def __init__(self) -> None:
        self.warmed = False
        self.calls = 0

    def warmup(self) -> None:
        self.warmed = True

    def synthesize(self, synthesis_request: SynthesisRequest) -> tuple[bytes, int]:
        del synthesis_request
        self.calls += 1
        return wav_bytes(), 24000


def test_lifespan_warms_once_and_health_means_real_warmup() -> None:
    engine = FakeEngine()
    with TestClient(
        create_qwen_app(engine, "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "sha256:" + "a" * 64)
    ) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ready"
        assert health.json()["warmed"] is True
    assert engine.warmed is True


def test_synthesize_returns_wav_and_integrity_headers() -> None:
    engine = FakeEngine()
    with TestClient(
        create_qwen_app(engine, "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "sha256:" + "a" * 64)
    ) as client:
        response = client.post("/v1/synthesize", json=request().model_dump())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["x-request-id"] == "req-service-1"
    assert response.headers["x-audio-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert engine.calls == 1


def test_rejects_request_model_checksum_mismatch() -> None:
    engine = FakeEngine()
    app = create_qwen_app(engine, "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "sha256:" + "b" * 64)
    with TestClient(app) as client:
        response = client.post("/v1/synthesize", json=request().model_dump())
    assert response.status_code == 409
    assert response.json()["detail"] == "request model checksum does not match loaded model"
    assert engine.calls == 0


def test_app_refuses_non_loopback_bind_configuration() -> None:
    from scripts.run_qwen_tts_service import validate_bind_host

    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    for invalid in ("0.0.0.0", "::", "192.168.1.5"):
        try:
            validate_bind_host(invalid)
        except ValueError as exc:
            assert "loopback" in str(exc)
        else:
            raise AssertionError(f"accepted non-loopback host {invalid}")


def test_model_engine_adapts_station_language_and_qwen_tuple(tmp_path) -> None:
    (tmp_path / "maya.pt").write_bytes(b"prompt")
    (tmp_path / "maya.wav").write_bytes(wav_bytes())

    class FakeQwenModel:
        def __init__(self) -> None:
            self.arguments = None

        def generate_voice_clone(
            self,
            text,
            language,
            ref_audio=None,
            ref_text=None,
            instruct=None,
            clone_prompt_path=None,
        ):
            self.arguments = {
                "text": text,
                "language": language,
                "ref_audio": ref_audio,
                "ref_text": ref_text,
                "instruct": instruct,
                "clone_prompt_path": clone_prompt_path,
            }
            return [np.zeros(2400, dtype=np.float32)], 24000

    engine = QwenModelEngine.__new__(QwenModelEngine)
    engine._voice_root = tmp_path.resolve(strict=True)
    engine._model = FakeQwenModel()

    payload, sample_rate = engine.synthesize(request())

    assert engine._model.arguments["language"] == "English"
    assert payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"
    assert sample_rate == 24000
    with wave.open(io.BytesIO(payload), "rb") as output:
        assert output.getnchannels() == 1
        assert output.getnframes() == 2400
