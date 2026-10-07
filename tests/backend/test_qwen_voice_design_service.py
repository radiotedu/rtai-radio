from __future__ import annotations

import json
import threading
from urllib.request import Request, urlopen

from scripts.run_qwen_voice_design_service import (
    MAX_NEW_TOKENS,
    SynthesisResult,
    VOICE_DESIGNS,
    VoiceDesignEngine,
    create_server,
    generated_codec_token_count,
    generation_budget,
)


class FakeEngine:
    model_path = "C:/qwen"
    model_name = "Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    engine_name = "qwen-voicedesign"
    cpu_threads = 3
    last_error = ""
    last_generated_sha256 = "abc"
    last_codec_tokens = 999
    last_generation_hit_ceiling = True

    def queue_snapshot(self):
        return {"busy": False, "context_waiters": 0, "queue_waiters": 0}

    def synthesize(
        self,
        text: str,
        language: str,
        design_id: str,
        seed: int,
        priority: str,
        is_cancelled=None,
    ) -> SynthesisResult:
        assert text == "Radio TED U"
        assert language == "en"
        assert design_id == "en-night"
        assert seed == 7
        assert priority == "context"
        return SynthesisResult(
            payload=b"RIFF" + b"\x00" * 4 + b"WAVE" + b"qwen",
            generation_ceiling=512,
            codec_tokens=120,
            hit_ceiling=False,
        )


def test_voice_design_policy_is_warm_at_night_and_never_whispers() -> None:
    night = VOICE_DESIGNS["en-night"].casefold()
    assert "terrestrial music-radio" in night
    assert "ordinary human breath groups" in night
    assert "never whisper" in night
    assert "1.2 to 1.5 words per second" in night
    assert "never draw out vowels" in night


def test_loopback_service_identifies_qwen_and_no_fallback() -> None:
    server = create_server("127.0.0.1", 0, FakeEngine())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/health", timeout=2) as response:
            health = json.load(response)
        assert health["engine"] == "qwen-voicedesign"
        assert health["fallback_tts"] is False
        assert health["cpu_threads"] == 3
        assert health["max_new_tokens"] == 512

        request = Request(
            base + "/v1/synthesize",
            data=json.dumps({
                "text": "Radio TED U",
                "language": "en",
                "design_id": "en-night",
                "seed": 7,
                "priority": "context",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            payload = response.read()
            assert response.headers["X-TTS-Engine"] == "qwen-voicedesign"
            assert response.headers["X-TTS-Fallback"] == "false"
            assert response.headers["X-TTS-Max-New-Tokens"] == "512"
            assert response.headers["X-TTS-Codec-Tokens"] == "120"
            assert response.headers["X-TTS-Hit-Ceiling"] == "false"
        assert payload.startswith(b"RIFF")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_generation_ceiling_cannot_cut_validated_radio_copy() -> None:
    assert MAX_NEW_TOKENS == 512
    assert generation_budget("A short Radio Ted You link.") == 288
    assert 432 <= generation_budget(" ".join(["word"] * 30)) <= 512


def test_codec_completion_counts_time_axis_not_parallel_codebooks() -> None:
    class Codes:
        shape = (491, 16)

    assert generated_codec_token_count(([Codes()], None)) == 491


def test_runaway_is_rejected_before_waveform_decode() -> None:
    source = __import__("pathlib").Path(
        __import__("scripts.run_qwen_voice_design_service", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert source.index("reached its codec ceiling before natural EOS") < source.index(
        "generate_voice_design(**generation)"
    )


def test_disconnected_waiter_is_removed_before_model_generation() -> None:
    engine = VoiceDesignEngine.__new__(VoiceDesignEngine)
    engine._condition = threading.Condition()
    engine._busy = True
    engine._context_waiters = 0
    engine._queue_waiters = 0

    try:
        engine.synthesize(
            text="Radio TED U",
            language="en",
            design_id="en-day",
            seed=1,
            priority="context",
            is_cancelled=lambda: True,
        )
    except ConnectionAbortedError:
        pass
    else:
        raise AssertionError("disconnected queued request was not cancelled")

    assert engine._context_waiters == 0
    assert engine._queue_waiters == 0
