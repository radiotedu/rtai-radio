from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from scripts import dynamic_host_queue as host
from scripts.dynamic_host_queue import (
    DynamicHostQueue,
    QueuedHostClip,
    generate_line,
    grounded_radio_link,
    normalize_station_name,
    completed_host_wav_rejection,
    valid_line,
)


def queue_for(tmp_path: Path, **overrides) -> DynamicHostQueue:
    values = {
        "language": "en",
        "voice_design": "en-day",
        "night_voice_design": "en-night",
        "qwen_url": "http://127.0.0.1:8090",
        "model": "model",
        "ollama_url": "http://127.0.0.1",
        "root": tmp_path,
        "stop": threading.Event(),
    }
    values.update(overrides)
    return DynamicHostQueue(**values)


def test_line_policy_rejects_routine_or_malformed_speech() -> None:
    assert valid_line("A strange little rhythm can reshape an ordinary afternoon.", "en")
    assert valid_line("Une mélodie étrange peut changer la couleur de cette journée.", "fr")
    assert valid_line("A strange little rhythm can reshape an ordinary afternoon.", "fr") is None
    assert valid_line("Good morning everyone, welcome back to the station today.") is None
    assert valid_line('{"line": "not plain speech"}') is None
    assert valid_line("This captivating sonic journey is the perfect soundtrack for your day.") is None
    assert valid_line("Radio TED U is back! We're playing This Love by Maroon 5.", "en") is None
    assert valid_line(
        "Radio TED U once back-announced the previous track Blue Room by Alice Example.",
        "en",
    ) is None
    assert valid_line(
        "Radio TED U once said Blue Room by Alice Example and Night Study by The Ensemble.",
        "en",
    ) is None


def test_announcement_metadata_exposes_models_and_well_wish_without_paths(tmp_path: Path) -> None:
    queue = queue_for(tmp_path / "queue")
    queue.root.mkdir(parents=True)
    clip = queue.root / "host-000000000002-123.wav"
    clip.with_suffix(".json").write_text(
        json.dumps(
            {
                "tts_engine": "kokoro",
                "tts_model": "Kokoro-82M",
                "text_model": "qwen3:0.6b",
                "voice_design": "en-day",
                "line": "That was a song, and here is another one for you to enjoy.",
            }
        ),
        encoding="utf-8",
    )

    metadata = queue.announcement_metadata(clip)

    assert metadata == {
        "text_model": "qwen3:0.6b",
        "tts_engine": "kokoro",
        "tts_model": "Kokoro-82M",
        "voice_design": "en-day",
        "well_wish": True,
    }


def test_grounded_radio_link_names_both_tracks_without_ai_filler() -> None:
    line = grounded_radio_link(
        "en",
        1,
        previous_title="Blue Room",
        previous_artist="Alice Example",
        next_title="Night Study",
        next_artist="The Ensemble",
    )

    assert "Blue Room" in line and "Alice Example" in line
    assert "Night Study" in line and "The Ensemble" in line
    assert "Radio Ted You" in line
    assert valid_line(line, "en") == line


def test_grounded_english_link_with_that_was_on_and_with_is_valid() -> None:
    line = (
        "That was Lady Gaga with Abracadabra. On Radio TED U: "
        "Von dutch by Charli xcx."
    )
    assert valid_line(line, "en") == line


def test_contextual_voice_seed_varies_by_durable_queue_sequence() -> None:
    source = Path(host.__file__).read_text(encoding="utf-8")
    assert "synthesis_seed=self.seed + (item.sequence * 7_919)" in source


def test_ready_clip_airs_after_three_or_four_songs_and_is_deleted(tmp_path: Path, monkeypatch) -> None:
    queue = queue_for(tmp_path, lead_min=3, lead_max=4, seed=99)
    clip = tmp_path / "host-test.wav"
    clip.write_bytes(b"audio")
    queue.ready.append(QueuedHostClip(clip, 0))
    queue.songs_remaining = 3
    queue.prepared_sequence = 0
    assert queue.after_song() is None
    assert queue.after_song() is None
    assert queue.after_song() == clip
    queue.played(clip)
    assert not clip.exists()


def test_unprepared_generic_clip_never_airs_and_music_continues(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    clip = tmp_path / "host-generic.wav"
    clip.write_bytes(b"generic qwen speech")
    queue.ready.append(QueuedHostClip(clip, 4))
    queue.songs_remaining = 1

    assert queue.after_song() is None
    assert queue.snapshot()["songs_until_air"] == 1
    assert clip.exists()


def test_every_song_mode_skips_an_expired_unprepared_slot(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    expired = tmp_path / "host-expired.wav"
    following = tmp_path / "host-following.wav"
    queue.ready.extend((QueuedHostClip(expired, 4), QueuedHostClip(following, 5)))
    queue.prepared_sequences.add(5)
    queue.songs_remaining = 1

    assert queue.after_song(skip_unprepared=True) is None
    assert queue.ready[0].sequence == 5
    assert queue.snapshot()["song_context_ready"] is True


def write_mono_wav(path: Path) -> None:
    import soundfile

    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(path, np.zeros(2400, dtype=np.float32), 24000, format="WAV")


def write_completed_host_wav(path: Path, duration_seconds: float = 8.0) -> None:
    import soundfile

    sample_rate = 24000
    sample_count = int(sample_rate * duration_seconds)
    timeline = np.arange(sample_count, dtype=np.float32) / sample_rate
    audio = (0.12 * np.sin(2 * np.pi * 220 * timeline)).astype(np.float32)
    audio[-int(sample_rate * 0.4):] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(path, audio, sample_rate, format="WAV", subtype="PCM_16")


def make_pool_clip(tmp_path: Path, design: str = "en-night", index: int = 0) -> Path:
    audio = tmp_path / "pool" / design / f"liner-{index:03d}.wav"
    write_mono_wav(audio)
    atomic_sidecar = audio.with_suffix(".json")
    atomic_sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "language": "en",
                "line": host.LINERS["en"][index],
                "text_model": host.QWEN_TEXT_MODEL,
                "tts_engine": "kokoro",
                "tts_model": host.TTS_MODEL_NAME,
            }
        ),
        encoding="utf-8",
    )
    return audio


def test_every_liner_copy_passes_the_valid_line_policy() -> None:
    for language, lines in host.LINERS.items():
        assert len(lines) >= host.LINER_POOL_TARGET
        for line in lines:
            assert host.valid_line(line, language) == line


def test_every_song_mode_keeps_generic_liners_off_air(
    tmp_path: Path,
) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    unprepared = tmp_path / "host-un.wav"
    slot_audio = tmp_path / "host-slot.wav"
    queue.ready.append(QueuedHostClip(unprepared, 4))
    queue.songs_remaining = 1
    design = queue._current_design
    liner = make_pool_clip(tmp_path, design=design, index=0)

    aired = queue.after_song(skip_unprepared=True)
    assert aired is None
    # The exact-context slot is discarded, but generic evergreen copy is never
    # substituted for a song-grounded announcement.
    assert not unprepared.exists()
    assert liner.exists()
    queue.played(liner)
    assert liner.exists()
    write_mono_wav(slot_audio)
    queue.played(slot_audio)
    assert not slot_audio.exists()


def test_liner_pool_fills_missing_templates_with_qwen_only_speech(
    tmp_path: Path, monkeypatch
) -> None:
    requests: list[dict] = []

    def fake_synthesize(text, design_id, target, **kwargs):
        requests.append(
            {"text": text, "design": design_id, "priority": kwargs["priority"]}
        )
        write_mono_wav(target)

    monkeypatch.setattr(host, "_synthesize", fake_synthesize)
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    assert queue._fill_pool_once() is True
    assert len(requests) == 1
    assert requests[0]["priority"] == "queue"
    assert requests[0]["text"] in host.LINERS["en"]
    ready = queue._pool_ready(queue._current_design)
    assert len(ready) == 1
    assert queue.liner_pool_snapshot()["ready"] == 1


def test_next_liner_clip_returns_none_while_pool_is_empty(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    assert queue.next_liner_clip() is None


def test_startup_buffer_counts_only_contiguous_ready_announcements(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, queue_target=3, startup_min=2)
    queue.ready.extend(
        QueuedHostClip(tmp_path / f"host-{sequence}.wav", sequence)
        for sequence in (1, 2, 3)
    )
    queue.prepared_sequences.update({2, 3})
    assert queue.snapshot()["prepared_ahead_count"] == 0
    queue.prepared_sequences.add(1)
    assert queue.snapshot()["prepared_ahead_count"] == 3


def test_scheduled_host_horizon_tracks_three_to_four_song_boundaries(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, lead_min=3, lead_max=4, queue_target=3)
    queue.ready.extend(
        QueuedHostClip(tmp_path / f"host-{sequence}.wav", sequence)
        for sequence in (0, 1, 2)
    )
    queue.songs_remaining = 2

    assert queue.scheduled_song_offsets() == [1, 5, 8]
    assert queue.snapshot()["scheduled_song_offsets"] == [1, 5, 8]


def test_explicit_zero_startup_buffer_allows_immediate_music_start(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, queue_target=10, startup_min=0)
    assert queue.snapshot()["startup_min"] == 0


def test_unsafe_metadata_is_never_queued_for_speech(tmp_path: Path) -> None:
    queue = queue_for(tmp_path, queue_target=1)
    queue.ready.append(QueuedHostClip(tmp_path / "host-1.wav", 1))
    queue.songs_remaining = 1

    assert not queue.prepare_for_song(
        title="08", artist="Anonymous420", next_title="Suburb", next_artist="Komiku"
    )
    assert queue.snapshot()["preparing_count"] == 0


def test_multiple_future_song_links_can_be_prepared_in_parallel(tmp_path: Path, monkeypatch) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1, queue_target=2)
    tmp_path.mkdir(parents=True, exist_ok=True)
    queue.ready.extend((
        QueuedHostClip(tmp_path / "host-000000000001-test.wav", 1),
        QueuedHostClip(tmp_path / "host-000000000002-test.wav", 2),
    ))
    queue.songs_remaining = 1
    monkeypatch.setattr(
        host,
        "generate_line",
        lambda *_args, previous_title, previous_artist, next_title, next_artist, **_kwargs: (
            f"That was {previous_title} by {previous_artist}. You're with Radio TED U; "
            f"next is {next_title} by {next_artist}."
        ),
    )
    monkeypatch.setattr(
        host,
        "_synthesize",
        lambda _text, _design, target, **_kwargs: write_completed_host_wav(target),
    )

    assert queue.prepare_for_song(
        title="First", artist="One", next_title="Second", next_artist="Two", offset=0
    )
    assert queue.prepare_for_song(
        title="Second", artist="Two", next_title="Third", next_artist="Three", offset=1
    )
    for thread in list(queue.context_threads.values()):
        thread.join(timeout=2)

    assert queue.prepared_sequences == {1, 2}


def test_sequence_is_persistent_and_deterministic(tmp_path: Path) -> None:
    first = queue_for(tmp_path, seed=12)
    assert first._next_sequence() == 0
    assert first._next_sequence() == 1
    second = queue_for(tmp_path, seed=12)
    assert second._next_sequence() == 2


def test_atomic_queue_persistence_survives_concurrent_writers(tmp_path: Path) -> None:
    target = tmp_path / "queue.json"
    errors: list[Exception] = []

    def write(worker: int) -> None:
        try:
            for sequence in range(30):
                host.atomic_text_replace(
                    target,
                    json.dumps({"worker": worker, "sequence": sequence}),
                )
        except Exception as exc:  # pragma: no cover - assertion records thread failures
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8"))["worker"] in range(8)
    assert list(tmp_path.glob(".queue.json.*.tmp")) == []


def test_queue_keeps_ten_durable_clips_across_restart(tmp_path: Path) -> None:
    first = queue_for(tmp_path, queue_target=10)
    tmp_path.mkdir(parents=True, exist_ok=True)
    for sequence in range(10):
        clip = tmp_path / f"host-{sequence:012d}-test.wav"
        clip.write_bytes(b"a" * 2048)
        first.ready.append(QueuedHostClip(clip, sequence))
    first.songs_remaining = 3
    with first.lock:
        first._persist_locked()

    second = queue_for(tmp_path, queue_target=10)
    second._restore()
    assert second.snapshot()["queue_depth"] == 10
    assert second.snapshot()["songs_until_air"] == 3


def test_station_name_is_spaced_for_ted_like_bed_and_u_like_you() -> None:
    assert normalize_station_name("Stay with RadioTEDU tonight.") == (
        "Stay with Radio Ted You tonight."
    )
    assert normalize_station_name("Radio TED-U") == "Radio Ted You"


def test_contextual_link_uses_grounded_qwen3_06b_text(monkeypatch) -> None:
    requests = []

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, **_kwargs):
        requests.append(json.loads(request.data))
        script = json.dumps({"script": "{previous_artist} with {previous_title}, just heard on {station}. Now it's {next_title} from {next_artist}."})
        return FakeResponse(json.dumps({"message": {"content": script}}).encode())

    monkeypatch.setattr(host, "urlopen", fake_urlopen)
    line = generate_line(
        "en",
        "http://127.0.0.1:11434",
        host.QWEN_TEXT_MODEL,
        3,
        9,
        previous_title="Blue Room",
        previous_artist="Alice Example",
        next_title="Night Study",
        next_artist="The Ensemble",
        daypart="night",
    )

    assert "Blue Room" in line and "Alice Example" in line
    assert "Night Study" in line and "The Ensemble" in line
    assert "Radio Ted You" in line
    assert line.startswith("Alice Example with Blue Room")
    assert len(requests) == 1
    assert requests[0]["model"] == host.QWEN_TEXT_MODEL
    assert requests[0]["think"] is False
    prompt = requests[0]["messages"][1]["content"]
    assert "Blue Room by Alice Example" in prompt
    assert "Night Study by The Ensemble" in prompt
    assert "allowed_bridges" not in prompt
    assert requests[0]["format"]["additionalProperties"] is False


def test_contextual_link_keeps_verified_catalogue_copy_when_small_text_model_paraphrases(monkeypatch) -> None:
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, **_kwargs):
        del request
        content = json.dumps({"bridge": "A generic radio sentence."})
        return FakeResponse(json.dumps({"message": {"content": content}}).encode())

    monkeypatch.setattr(host, "urlopen", fake_urlopen)
    line = generate_line(
        "en",
        "http://127.0.0.1:11434",
        host.QWEN_TEXT_MODEL,
        3,
        9,
        previous_title="Blue Room",
        previous_artist="Alice Example",
        next_title="Night Study",
        next_artist="The Ensemble",
        daypart="night",
    )

    assert "Blue Room" in line and "Alice Example" in line
    assert "Night Study" in line and "The Ensemble" in line
    assert "Radio Ted You" in line


@pytest.mark.parametrize("language", ["en", "fr"])
def test_offline_links_avoid_recent_scripts_and_vary_wishes(monkeypatch, language) -> None:
    def offline(*_args, **_kwargs):
        raise OSError("text runtime unavailable")
    monkeypatch.setattr(host, "urlopen", offline)
    facts = dict(previous_title="Blue Room", previous_artist="Alice Example",
                 next_title="Night Study", next_artist="The Ensemble")
    history = []
    for sequence in range(36):
        line = generate_line(language, "http://unused", host.QWEN_TEXT_MODEL, sequence, 5,
                             daypart="night", recent_scripts=tuple(history), **facts)
        script = host.announcement_script(line, station=host.SPOKEN_STATION_NAME, **facts)
        assert not host.repeated_script(script, tuple(history))
        assert all(value in line for value in facts.values())
        history.append(script)


def test_audio_cache_is_unique_to_each_announcement_sequence(tmp_path) -> None:
    queue = queue_for(tmp_path)
    facts = dict(design_id="en-night", title="Blue Room", artist="Alice",
                 next_title="Night Study", next_artist="Bob", well_wish=False)
    assert queue._context_key(sequence=1, **facts) != queue._context_key(sequence=2, **facts)


def test_shifted_schedule_cannot_prepare_the_wrong_slot(tmp_path) -> None:
    queue = queue_for(tmp_path)
    queue.ready.append(QueuedHostClip(tmp_path / "host-000000000002-test.wav", 2))
    queue.songs_remaining = 1
    assert not queue.prepare_for_song(title="Blue Room", artist="Alice",
                                      next_title="Night Study", next_artist="Bob",
                                      expected_sequence=1)
    assert not queue.preparing_sequences


def test_recent_copy_history_survives_restart(tmp_path) -> None:
    scripts = ["{previous_artist} with {previous_title}, just heard. {station} brings you {next_title} by {next_artist}."]
    (tmp_path / "announcement-history.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
    assert list(queue_for(tmp_path).recent_scripts) == scripts


def test_final_air_check_rejects_stale_song_context(tmp_path) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    clip = tmp_path / "host-000000000001-stale.wav"
    queue.ready.append(QueuedHostClip(clip, 1))
    queue.songs_remaining = 1
    queue.prepared_sequences.add(1)
    clip.with_suffix(".json").write_text(json.dumps({
        "copy_version": host.ANNOUNCEMENT_COPY_VERSION,
        "previous_title": "Wrong Song", "previous_artist": "Alice",
        "next_title": "Night Study", "next_artist": "Bob",
    }), encoding="utf-8")
    assert queue.after_song(expected_song_context={
        "previous_title": "Blue Room", "previous_artist": "Alice",
        "next_title": "Night Study", "next_artist": "Bob",
    }) is None
    assert "stale announcement" in queue.last_error


def test_final_air_check_accepts_matching_song_context(tmp_path) -> None:
    queue = queue_for(tmp_path, lead_min=1, lead_max=1)
    clip = tmp_path / "host-000000000001-match.wav"
    queue.ready.append(QueuedHostClip(clip, 1))
    queue.songs_remaining = 1
    queue.prepared_sequences.add(1)
    facts = dict(previous_title="Blue Room", previous_artist="Alice",
                 next_title="Night Study", next_artist="Bob")
    clip.with_suffix(".json").write_text(json.dumps({
        "copy_version": host.ANNOUNCEMENT_COPY_VERSION, **facts,
    }), encoding="utf-8")
    assert queue.after_song(expected_song_context=facts) == clip


def test_synthesis_uses_local_kokoro_and_writes_wav(tmp_path: Path, monkeypatch) -> None:
    calls = []
    audio = io.BytesIO()
    import soundfile
    sample_rate = 24000
    timeline = np.arange(sample_rate * 3, dtype=np.float32) / sample_rate
    waveform = (0.12 * np.sin(2 * np.pi * 220 * timeline)).astype(np.float32)
    waveform[-int(sample_rate * 0.4):] = 0
    soundfile.write(audio, waveform, sample_rate, format="WAV", subtype="PCM_16")

    class FakeResponse:
        headers = {
            "X-TTS-Engine": "kokoro",
            "X-TTS-Fallback": "false",
            "X-TTS-Complete": "true",
            "X-TTS-Hit-Ceiling": "false",
            "X-TTS-Rendered-Chunks": "1",
        }
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _limit):
            return audio.getvalue()

    def fake_urlopen(request, timeout):
        calls.append((json.loads(request.data), timeout))
        return FakeResponse()

    monkeypatch.setattr(host, "urlopen", fake_urlopen)
    target = tmp_path / "host.wav"
    host._synthesize(
        "You're with RadioTEDU after this song.",
        "en-night",
        target,
        language="en",
        qwen_url="http://127.0.0.1:8090",
        synthesis_seed=7,
        priority="context",
    )

    assert target.read_bytes()[:4] == b"RIFF"
    # Qwen receives the pronunciation-safe expansion; public metadata keeps
    # the branded spelling.
    assert calls[0][0]["text"] == "You're with Radio Ted You after this song."
    assert calls[0][0]["language"] == "en"
    assert calls[0][0]["design_id"] == "en-night"
    assert calls[0][0]["priority"] == "context"


def test_completed_audio_gate_accepts_speech_with_a_quiet_finish(tmp_path: Path) -> None:
    target = tmp_path / "complete.wav"
    write_completed_host_wav(target, duration_seconds=4.0)

    assert completed_host_wav_rejection(
        target, "That was Blue Room. Radio Ted You continues with Night Study."
    ) is None


def test_completed_audio_gate_rejects_an_active_cutoff(tmp_path: Path) -> None:
    target = tmp_path / "cut.wav"
    import soundfile

    sample_rate = 24000
    timeline = np.arange(sample_rate * 4, dtype=np.float32) / sample_rate
    waveform = (0.12 * np.sin(2 * np.pi * 220 * timeline)).astype(np.float32)
    soundfile.write(target, waveform, sample_rate, format="WAV", subtype="PCM_16")

    rejection = completed_host_wav_rejection(
        target, "That was Blue Room. Radio Ted You continues with Night Study."
    )
    assert rejection is not None and "unfinished speech tail" in rejection


def test_completed_audio_gate_rejects_audio_too_short_for_copy(tmp_path: Path) -> None:
    target = tmp_path / "short.wav"
    write_completed_host_wav(target, duration_seconds=2.0)

    rejection = completed_host_wav_rejection(
        target,
        "That was a beautiful long song by Alice Example and now Radio Ted You continues with another full track by The Ensemble tonight.",
    )
    assert rejection is not None and "ended too early" in rejection


def test_completed_audio_gate_rejects_drawn_out_distressed_delivery(tmp_path: Path) -> None:
    target = tmp_path / "drawn-out.wav"
    write_completed_host_wav(target, duration_seconds=23.0)

    rejection = completed_host_wav_rejection(
        target,
        "Lady Gaga, Abracadabra. Radio Ted You. Next: Maroon 5, This Love.",
    )
    assert rejection is not None and "ran beyond the safe speech duration" in rejection


def test_completed_audio_gate_allows_subsecond_framing_tolerance(tmp_path: Path) -> None:
    target = tmp_path / "warm-boundary.wav"
    write_completed_host_wav(target, duration_seconds=15.28)

    assert completed_host_wav_rejection(
        target,
        "Lady Gaga, Abracadabra. Radio Ted You. Next: Maroon 5, This Love.",
    ) is None


def test_synthesis_rejects_piper_even_when_it_claims_no_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeResponse:
        headers = {"X-TTS-Engine": "piper-local", "X-TTS-Fallback": "false"}
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(host, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    with pytest.raises(RuntimeError, match="not Kokoro"):
        host._synthesize(
            "This is Radio TED U.",
            "en-day",
            tmp_path / "host.wav",
            language="en",
            qwen_url="http://127.0.0.1:8090",
            synthesis_seed=7,
            priority="context",
        )


def test_source_has_no_non_qwen_tts_provider() -> None:
    source = Path(host.__file__).read_text(encoding="utf-8")
    assert "edge_tts" not in source
    assert "/v1/synthesize" in source


def test_restart_rechecks_persisted_announcement_contexts(tmp_path):
    queue = queue_for(tmp_path, queue_target=10)
    clip = tmp_path / "host-000000000001-test.wav"
    clip.write_bytes(b"existing-validated-audio")
    queue.ready.append(QueuedHostClip(clip, 1))
    queue.songs_remaining = 3
    queue.prepared_sequences.add(1)
    queue.prepared_contexts[1] = "previous-song-pair"
    queue.invalidate_restored_song_contexts()
    assert queue.snapshot()["queue_depth"] == 1
    assert queue.snapshot()["song_context_ready"] is False
    assert queue.prepared_contexts == {}
    assert clip.exists()
