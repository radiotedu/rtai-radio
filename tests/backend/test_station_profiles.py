from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import subprocess

import pytest

from backend.config import Settings
from backend.stations.context import (
    StationContext,
    build_station_context,
    coerce_station_context,
    english_compatibility_context,
    ensure_station_runtime_dirs,
)
from backend.stations.loader import StationProfileError, load_station_profile, load_station_profiles
from backend.stations.models import AudioProfile, PublicProfile, RuntimeProfile, StationProfile


def make_profile(station_id: str = "radiotedu-en") -> StationProfile:
    return StationProfile(
        profile_version=1,
        station_id=station_id,
        display_name="RadioTEDU",
        language="en",
        locale="en-US",
        timezone="Europe/Istanbul",
        public=PublicProfile(
            route="/ai",
            compatibility_routes=(),
            snapshot_endpoint=f"/v1/radio/stations/{station_id}/snapshot",
            status_endpoint=f"/v1/radio/stations/{station_id}/status",
            stream_url="https://stream.radiotedu.com/en",
        ),
        audio=AudioProfile("/en", -16, -1, 5),
        runtime=RuntimeProfile(
            f"data/stations/{station_id}",
            f"data/stations/{station_id}/radio.db",
            f"media/stations/{station_id}/music",
            f"data/stations/{station_id}/announcements",
            f"data/stations/{station_id}/qwen-cache",
            f"data/stations/{station_id}/logs",
        ),
        voice_pack=f"{station_id}-voices-v1",
        snapshot_secret_ref="RADIOTEDU_EN_SNAPSHOT_SECRET",
    )


def test_station_profile_is_nested_and_immutable() -> None:
    profile = make_profile()
    assert profile.public.compatibility_routes == ()
    assert profile.audio.minimum_qwen_buffer == 5
    with pytest.raises(FrozenInstanceError):
        profile.display_name = "changed"  # type: ignore[misc]


def test_context_derives_station_scoped_settings() -> None:
    profiles = load_station_profiles("config/stations")
    settings = Settings()

    context = build_station_context(settings, profiles["radiotedu-fr"])

    assert context.profile.station_id == "radiotedu-fr"
    assert context.settings is not settings
    assert settings.database_path == "data/radiotedu.db"
    assert context.settings.database_path.endswith("data/stations/radiotedu-fr/radio.db")
    assert context.settings.liquidsoap_host == "10.98.98.75"
    assert context.settings.liquidsoap_port == 11154
    assert context.settings.liquidsoap_mount == "/fr"
    assert context.settings.liquidsoap_icecast_user == "source"
    assert context.settings.liquidsoap_encoder_profile == "aac_192"
    assert context.settings.liquidsoap_public is True
    assert context.announcement_root != context.cache_root


def test_context_resolution_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "radiotedu-fr"
    profile = replace(
        load_station_profiles("config/stations")["radiotedu-fr"],
        runtime=RuntimeProfile(
            str(root),
            str(root / "radio.db"),
            str(tmp_path / "media" / "radiotedu-fr"),
            str(root / "announcements"),
            str(root / "qwen-cache"),
            str(root / "logs"),
        ),
    )

    context = build_station_context(Settings(), profile)

    assert context.data_root == root.resolve()
    assert context.database_file == (root / "radio.db").resolve()
    assert context.music_root == (tmp_path / "media" / "radiotedu-fr").resolve()
    assert context.announcement_root == (root / "announcements").resolve()
    assert context.cache_root == (root / "qwen-cache").resolve()
    assert context.log_root == (root / "logs").resolve()
    assert not root.exists()
    assert not (tmp_path / "media").exists()


def test_runtime_directories_are_created_only_by_explicit_ensure(tmp_path: Path) -> None:
    deployment_root = tmp_path / "deployment"
    deployment_root.mkdir()
    root = deployment_root / "data" / "stations" / "radiotedu-en"
    profile = replace(
        make_profile(),
        runtime=RuntimeProfile(
            "data/stations/radiotedu-en",
            "data/stations/radiotedu-en/radio.db",
            "media/stations/radiotedu-en/music",
            "data/stations/radiotedu-en/announcements",
            "data/stations/radiotedu-en/qwen-cache",
            "data/stations/radiotedu-en/logs",
        ),
    )
    previous = Path.cwd()
    os.chdir(deployment_root)
    try:
        context = build_station_context(Settings(), profile)
        assert not root.exists()

        ensure_station_runtime_dirs(context)

        assert context.data_root.is_dir()
        assert context.database_file.parent.is_dir()
        assert context.music_root.is_dir()
        assert context.announcement_root.is_dir()
        assert context.cache_root.is_dir()
        assert context.log_root.is_dir()
    finally:
        os.chdir(previous)


def _context_with_runtime(profile: StationProfile, runtime: RuntimeProfile) -> StationContext:
    return build_station_context(Settings(), replace(profile, runtime=runtime))


@pytest.mark.parametrize("data_root", ["../escape", "ABSOLUTE_ESCAPE"])
def test_runtime_ensure_rejects_data_root_escape_without_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_root: str,
) -> None:
    deployment_root = tmp_path / "deployment"
    deployment_root.mkdir()
    monkeypatch.chdir(deployment_root)
    escaped = tmp_path / "escape"
    selected_root = str(escaped.resolve()) if data_root == "ABSOLUTE_ESCAPE" else data_root
    runtime = RuntimeProfile(
        selected_root,
        f"{selected_root}/radio.db",
        "media/stations/radiotedu-en/music",
        f"{selected_root}/announcements",
        f"{selected_root}/qwen-cache",
        f"{selected_root}/logs",
    )
    context = _context_with_runtime(make_profile(), runtime)

    with pytest.raises(ValueError, match="data_root.*deployment root"):
        ensure_station_runtime_dirs(context)

    assert not escaped.exists()
    assert not (deployment_root / "media").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_path", "../escape/radio.db"),
        ("music_dir", "../escape/music"),
        ("static_dir", "../escape/static"),
        ("rss_feeds_path", "../escape/rss.json"),
        ("liquidsoap_queue_path", "../escape/queue.m3u"),
        ("liquidsoap_script_path", "../escape/radio.liq"),
    ],
)
def test_runtime_ensure_validates_every_settings_target_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    deployment_root = tmp_path / "deployment"
    deployment_root.mkdir()
    monkeypatch.chdir(deployment_root)
    profiles_dir = Path(__file__).resolve().parents[2] / "config" / "stations"
    context = build_station_context(Settings(), load_station_profiles(profiles_dir)["radiotedu-en"])
    setattr(context.settings, field, value)

    with pytest.raises(ValueError, match="runtime path"):
        ensure_station_runtime_dirs(context)

    assert not (tmp_path / "escape").exists()
    assert not (deployment_root / "data").exists()
    assert not (deployment_root / "media").exists()


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"could not create test junction: {result.stderr or result.stdout}")
    else:
        link.symlink_to(target, target_is_directory=True)


def test_runtime_ensure_rejects_existing_link_escape_before_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_root = tmp_path / "deployment"
    station_root = deployment_root / "data" / "stations" / "radiotedu-en"
    outside = tmp_path / "outside"
    station_root.mkdir(parents=True)
    outside.mkdir()
    _create_directory_link(station_root / "announcements", outside)
    monkeypatch.chdir(deployment_root)
    profiles_dir = Path(__file__).resolve().parents[2] / "config" / "stations"
    context = build_station_context(Settings(), load_station_profiles(profiles_dir)["radiotedu-en"])

    with pytest.raises(ValueError, match="announcement.*data_root"):
        ensure_station_runtime_dirs(context)

    assert list(outside.iterdir()) == []
    assert not (station_root / "qwen-cache").exists()
    assert not (station_root / "logs").exists()
    assert not (deployment_root / "media").exists()


def test_runtime_ensure_rejects_music_link_escape_before_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment_root = tmp_path / "deployment"
    music_parent = deployment_root / "media" / "stations" / "radiotedu-en"
    outside = tmp_path / "outside-music"
    music_parent.mkdir(parents=True)
    outside.mkdir()
    _create_directory_link(music_parent / "music", outside)
    monkeypatch.chdir(deployment_root)
    profiles_dir = Path(__file__).resolve().parents[2] / "config" / "stations"
    context = build_station_context(Settings(), load_station_profiles(profiles_dir)["radiotedu-en"])

    with pytest.raises(ValueError, match="music.*deployment root"):
        ensure_station_runtime_dirs(context)

    assert list(outside.iterdir()) == []
    assert not (deployment_root / "data").exists()


def test_english_compatibility_adapter_preserves_direct_settings() -> None:
    settings = Settings(
        database_path="legacy/radio.db",
        music_dir="legacy/music",
        static_dir="legacy/static",
        public_stream_url="https://example.test/legacy",
        min_ready_announcements=3,
    )

    context = english_compatibility_context(settings)

    assert context.settings is not settings
    assert context.settings == settings
    assert context.profile.station_id == "radiotedu-en"
    assert context.profile.language == "en"
    assert context.profile.locale == "en-US"
    assert context.profile.public.route == "/ai"
    assert context.profile.public.compatibility_routes == ()
    assert context.profile.public.stream_url == settings.public_stream_url
    assert context.profile.audio.minimum_qwen_buffer == 5
    coerced = coerce_station_context(settings)
    assert coerced.settings is not settings
    settings.database_path = "mutated/radio.db"
    assert context.settings.database_path == "legacy/radio.db"
    assert coerced.settings.database_path == "legacy/radio.db"
    assert coerce_station_context(context) is context


def test_english_compatibility_normalizes_copied_selector_without_mutating_caller() -> None:
    settings = Settings(station_id="radiotedu-fr")

    direct = english_compatibility_context(settings)
    coerced = coerce_station_context(settings)

    assert settings.station_id == "radiotedu-fr"
    for context in (direct, coerced):
        assert context.settings is not settings
        assert context.settings.station_id == "radiotedu-en"
        assert context.profile.station_id == "radiotedu-en"
        assert context.settings.station_id == context.profile.station_id


def test_compatibility_containment_mode_cannot_flip_after_settings_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = english_compatibility_context(Settings())
    context.settings.public_stream_url = "https://example.test/changed-after-construction"

    ensure_station_runtime_dirs(context)

    assert context.data_root == (tmp_path / "data").resolve()
    assert context.music_root == (tmp_path / "data" / "music").resolve()
    assert context.announcement_root == (
        tmp_path / "backend" / "static" / "generated" / "tts"
    ).resolve()


def test_english_compatibility_runtime_keeps_legacy_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = english_compatibility_context(Settings())

    ensure_station_runtime_dirs(context)

    assert context.database_file == (tmp_path / "data" / "radiotedu.db").resolve()
    assert context.music_root == (tmp_path / "data" / "music").resolve()
    assert context.announcement_root == (
        tmp_path / "backend" / "static" / "generated" / "tts"
    ).resolve()
    assert context.cache_root == (
        tmp_path / "backend" / "static" / "generated" / "tts" / "qwen-cache"
    ).resolve()
    assert context.log_root == (tmp_path / "data" / "logs").resolve()


def test_station_context_is_frozen() -> None:
    context = english_compatibility_context(Settings())
    with pytest.raises(FrozenInstanceError):
        context.profile = make_profile("radiotedu-fr")  # type: ignore[misc]


def test_station_selectors_load_from_environment_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STATION_ID=radiotedu-fr\nSTATION_PROFILES_DIR=private/stations\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.station_id == "radiotedu-fr"
    assert settings.station_profiles_dir == "private/stations"
    assert settings.station_profiles_path == Path("private/stations")


@pytest.mark.parametrize("station_id", ["radiotedu-de", "", "RadioTEDU-EN"])
def test_direct_settings_reject_unknown_station_selector(station_id: str) -> None:
    with pytest.raises(ValueError, match="station_id must be radiotedu-en or radiotedu-fr"):
        Settings(station_id=station_id)


def test_environment_file_rejects_unknown_station_selector(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("STATION_ID=radiotedu-de\n", encoding="utf-8")

    with pytest.raises(ValueError, match="station_id must be radiotedu-en or radiotedu-fr"):
        Settings.from_env(env_file)


def test_process_environment_rejects_unknown_station_selector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("STATION_ID", "radiotedu-de")

    with pytest.raises(ValueError, match="station_id must be radiotedu-en or radiotedu-fr"):
        Settings.from_env(tmp_path / "missing.env")


def test_canonical_profiles_have_frozen_identity() -> None:
    profiles = load_station_profiles(Path("config/stations"))

    assert set(profiles) == {"radiotedu-en", "radiotedu-fr"}
    assert profiles["radiotedu-en"] == StationProfile(
        profile_version=1,
        station_id="radiotedu-en",
        display_name="RadioTEDU",
        language="en",
        locale="en-US",
        timezone="Europe/Istanbul",
        public=PublicProfile(
            route="/ai",
            compatibility_routes=(),
            snapshot_endpoint="/v1/radio/stations/radiotedu-en/snapshot",
            status_endpoint="/v1/radio/stations/radiotedu-en/status",
            stream_url="https://stream.radiotedu.com/en",
        ),
        audio=AudioProfile(
            "/en",
            -16,
            -1,
            5,
            source_host="10.98.98.75",
            source_port=11154,
            source_user="source",
            encoder_profile="aac_192",
            public_listing=True,
        ),
        runtime=RuntimeProfile(
            "data/stations/radiotedu-en",
            "data/stations/radiotedu-en/radio.db",
            "media/stations/radiotedu-en/music",
            "data/stations/radiotedu-en/announcements",
            "data/stations/radiotedu-en/qwen-cache",
            "data/stations/radiotedu-en/logs",
        ),
        voice_pack="radiotedu-en-voices-v1",
        snapshot_secret_ref="RADIOTEDU_EN_SNAPSHOT_SECRET",
    )
    assert profiles["radiotedu-fr"] == StationProfile(
        profile_version=1,
        station_id="radiotedu-fr",
        display_name="RadioTEDU Français",
        language="fr",
        locale="fr-FR",
        timezone="Europe/Istanbul",
        public=PublicProfile(
            route="/ai",
            compatibility_routes=(),
            snapshot_endpoint="/v1/radio/stations/radiotedu-fr/snapshot",
            status_endpoint="/v1/radio/stations/radiotedu-fr/status",
            stream_url="https://stream.radiotedu.com/fr",
        ),
        audio=AudioProfile(
            "/fr",
            -16,
            -1,
            5,
            source_host="10.98.98.75",
            source_port=11154,
            source_user="source",
            encoder_profile="aac_192",
            public_listing=True,
        ),
        runtime=RuntimeProfile(
            "data/stations/radiotedu-fr",
            "data/stations/radiotedu-fr/radio.db",
            "media/stations/radiotedu-fr/music",
            "data/stations/radiotedu-fr/announcements",
            "data/stations/radiotedu-fr/qwen-cache",
            "data/stations/radiotedu-fr/logs",
        ),
        voice_pack="radiotedu-fr-voices-v1",
        snapshot_secret_ref="RADIOTEDU_FR_SNAPSHOT_SECRET",
    )


def test_profiles_share_only_the_single_listener_page() -> None:
    profiles = load_station_profiles("config/stations")
    english, french = profiles["radiotedu-en"], profiles["radiotedu-fr"]

    assert english.runtime.database != french.runtime.database
    assert english.runtime.cache_root != french.runtime.cache_root
    assert english.runtime.log_root != french.runtime.log_root
    assert english.audio.stream_mount != french.audio.stream_mount
    assert english.snapshot_secret_ref != french.snapshot_secret_ref
    assert english.public.route == french.public.route == "/ai"
    assert english.public.compatibility_routes == french.public.compatibility_routes == ()


def _canonical_raw(station_id: str = "radiotedu-en") -> dict[str, object]:
    return json.loads(Path(f"config/stations/{station_id}.json").read_text(encoding="utf-8"))


def _write_profile(directory: Path, name: str, raw: dict[str, object]) -> Path:
    path = directory / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_unknown_key_is_rejected_before_runtime_paths_are_created(tmp_path: Path) -> None:
    data_root = tmp_path / "must-not-exist"
    raw = _canonical_raw()
    raw["extra"] = True
    runtime = raw["runtime"]
    assert isinstance(runtime, dict)
    runtime["data_root"] = data_root.as_posix()

    profile_path = _write_profile(tmp_path, "bad.json", raw)
    with pytest.raises(StationProfileError, match="unknown keys: extra"):
        load_station_profile(profile_path)

    assert not data_root.exists()


def test_missing_and_unknown_nested_keys_are_rejected(tmp_path: Path) -> None:
    missing = _canonical_raw()
    del missing["voice_pack"]
    with pytest.raises(StationProfileError, match="missing keys: voice_pack"):
        load_station_profile(_write_profile(tmp_path, "missing.json", missing))

    nested = _canonical_raw()
    public = nested["public"]
    assert isinstance(public, dict)
    public["unexpected"] = "value"
    with pytest.raises(StationProfileError, match="public unknown keys: unexpected"):
        load_station_profile(_write_profile(tmp_path, "nested.json", nested))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("station_id", "RadioTEDU EN", "invalid station_id"),
        ("locale", "fr-FR", "language and locale do not agree"),
        ("timezone", "UTC", "timezone must be Europe/Istanbul"),
    ],
)
def test_identity_fields_are_strict(tmp_path: Path, field: str, value: object, message: str) -> None:
    raw = _canonical_raw()
    raw[field] = value
    with pytest.raises(StationProfileError, match=message):
        load_station_profile(_write_profile(tmp_path, f"bad-{field}.json", raw))


def test_scalar_values_are_not_coerced(tmp_path: Path) -> None:
    raw = _canonical_raw()
    raw["profile_version"] = True

    with pytest.raises(StationProfileError, match="profile.profile_version must be an integer"):
        load_station_profile(_write_profile(tmp_path, "coerced.json", raw))


@pytest.mark.parametrize(
    "mutation",
    [
        "display_name",
        "language_locale",
        "public_route",
        "compatibility_routes",
        "snapshot_endpoint",
        "status_endpoint",
        "stream_url",
        "stream_mount",
        "source_host",
        "source_port",
        "source_user",
        "encoder_profile",
        "public_listing",
        "minimum_qwen_buffer",
        "music_root",
        "voice_pack",
        "snapshot_secret_ref",
    ],
)
def test_frozen_station_identity_rejects_mixed_or_alternate_values(tmp_path: Path, mutation: str) -> None:
    raw = _canonical_raw("radiotedu-en")
    public = raw["public"]
    audio = raw["audio"]
    runtime = raw["runtime"]
    assert isinstance(public, dict)
    assert isinstance(audio, dict)
    assert isinstance(runtime, dict)
    mutations = {
        "display_name": lambda: raw.__setitem__("display_name", "RadioTEDU English"),
        "language_locale": lambda: (raw.__setitem__("language", "fr"), raw.__setitem__("locale", "fr-FR")),
        "public_route": lambda: public.__setitem__("route", "/ai/english"),
        "compatibility_routes": lambda: public.__setitem__("compatibility_routes", ["/ai-old"]),
        "snapshot_endpoint": lambda: public.__setitem__("snapshot_endpoint", "/v1/radio/stations/radiotedu-en/other"),
        "status_endpoint": lambda: public.__setitem__("status_endpoint", "/v1/radio/stations/radiotedu-en/other"),
        "stream_url": lambda: public.__setitem__("stream_url", "https://stream.radiotedu.com/other"),
        "stream_mount": lambda: audio.__setitem__("stream_mount", "/en-alt"),
        "source_host": lambda: audio.__setitem__("source_host", "127.0.0.1"),
        "source_port": lambda: audio.__setitem__("source_port", 8000),
        "source_user": lambda: audio.__setitem__("source_user", "alternate"),
        "encoder_profile": lambda: audio.__setitem__("encoder_profile", "mp3_192"),
        "public_listing": lambda: audio.__setitem__("public_listing", False),
        "minimum_qwen_buffer": lambda: audio.__setitem__("minimum_qwen_buffer", 6),
        "music_root": lambda: runtime.__setitem__("music_root", "media/stations/radiotedu-en/alternate"),
        "voice_pack": lambda: raw.__setitem__("voice_pack", "radiotedu-en-voices-v2"),
        "snapshot_secret_ref": lambda: raw.__setitem__("snapshot_secret_ref", "RADIOTEDU_EN_OTHER_SECRET"),
    }
    mutations[mutation]()

    with pytest.raises(StationProfileError, match="frozen identity"):
        load_station_profile(_write_profile(tmp_path, f"mixed-{mutation}.json", raw))


def test_regex_valid_but_unsupported_station_id_is_rejected(tmp_path: Path) -> None:
    raw = _canonical_raw("radiotedu-en")
    raw["station_id"] = "radiotedu-de"

    with pytest.raises(StationProfileError, match="unsupported station_id"):
        load_station_profile(_write_profile(tmp_path, "radiotedu-de.json", raw))


def test_profile_directory_requires_both_canonical_stations(tmp_path: Path) -> None:
    _write_profile(tmp_path, "radiotedu-en.json", _canonical_raw("radiotedu-en"))

    with pytest.raises(StationProfileError, match="profile set must contain exactly"):
        load_station_profiles(tmp_path)


def test_profile_directory_rejects_empty_and_extra_sets(tmp_path: Path) -> None:
    with pytest.raises(StationProfileError, match="profile set must contain exactly"):
        load_station_profiles(tmp_path)

    _write_profile(tmp_path, "radiotedu-en.json", _canonical_raw("radiotedu-en"))
    _write_profile(tmp_path, "radiotedu-fr.json", _canonical_raw("radiotedu-fr"))
    extra = json.loads(
        json.dumps(_canonical_raw("radiotedu-en"))
        .replace("radiotedu-en", "radiotedu-de")
        .replace("RADIOTEDU_EN", "RADIOTEDU_DE")
    )
    extra["display_name"] = "RadioTEDU Deutsch"
    extra_public = extra["public"]
    assert isinstance(extra_public, dict)
    extra_public["route"] = "/ai/de"
    extra_public["compatibility_routes"] = []
    _write_profile(tmp_path, "radiotedu-de.json", extra)

    with pytest.raises(StationProfileError, match="unsupported station_id|profile set must contain exactly"):
        load_station_profiles(tmp_path)


def test_duplicate_station_ids_are_rejected(tmp_path: Path) -> None:
    raw = _canonical_raw()
    _write_profile(tmp_path, "one.json", raw)
    _write_profile(tmp_path, "two.json", raw)

    with pytest.raises(StationProfileError, match="duplicate station_id"):
        load_station_profiles(tmp_path)


def _set_runtime_tree(raw: dict[str, object], data_root: str) -> None:
    runtime = raw["runtime"]
    assert isinstance(runtime, dict)
    root = data_root.rstrip("/\\")
    runtime.update(
        {
            "data_root": root,
            "database": f"{root}/radio.db",
            "announcement_root": f"{root}/announcements",
            "cache_root": f"{root}/qwen-cache",
            "log_root": f"{root}/logs",
        }
    )


def test_cross_field_writable_path_alias_is_rejected(tmp_path: Path) -> None:
    english = _canonical_raw("radiotedu-en")
    french = _canonical_raw("radiotedu-fr")
    root = (tmp_path / "shared").as_posix()
    _set_runtime_tree(english, root)
    _set_runtime_tree(french, f"{root}/radio.db")
    _write_profile(tmp_path, "radiotedu-en.json", english)
    _write_profile(tmp_path, "radiotedu-fr.json", french)

    with pytest.raises(StationProfileError, match="writable path overlap"):
        load_station_profiles(tmp_path)


def test_cross_station_ancestor_descendant_writable_paths_are_rejected(tmp_path: Path) -> None:
    english = _canonical_raw("radiotedu-en")
    french = _canonical_raw("radiotedu-fr")
    root = (tmp_path / "ancestor").as_posix()
    _set_runtime_tree(english, root)
    _set_runtime_tree(french, f"{root}/radio.db/archive")
    _write_profile(tmp_path, "radiotedu-en.json", english)
    _write_profile(tmp_path, "radiotedu-fr.json", french)

    with pytest.raises(StationProfileError, match="writable path overlap"):
        load_station_profiles(tmp_path)


def test_windows_case_alias_between_station_paths_is_rejected(tmp_path: Path) -> None:
    english = _canonical_raw("radiotedu-en")
    french = _canonical_raw("radiotedu-fr")
    _set_runtime_tree(english, "C:/RadioTEDU/ProfileA")
    _set_runtime_tree(french, "c:/radiotedu/profilea/RADIO.DB")
    _write_profile(tmp_path, "radiotedu-en.json", english)
    _write_profile(tmp_path, "radiotedu-fr.json", french)

    with pytest.raises(StationProfileError, match="writable path overlap"):
        load_station_profiles(tmp_path)


def test_station_writable_children_must_be_strict_descendants(tmp_path: Path) -> None:
    raw = _canonical_raw("radiotedu-en")
    runtime = raw["runtime"]
    assert isinstance(runtime, dict)
    runtime["database"] = runtime["data_root"]

    with pytest.raises(StationProfileError, match="strictly beneath runtime.data_root"):
        load_station_profile(_write_profile(tmp_path, "equal-root.json", raw))


@pytest.mark.parametrize(
    "field",
    ["database", "data_root", "music_root", "announcement_root", "cache_root", "log_root"],
)
def test_each_named_writable_field_collision_is_rejected(tmp_path: Path, field: str) -> None:
    english = _canonical_raw("radiotedu-en")
    french = _canonical_raw("radiotedu-fr")
    english_runtime = english["runtime"]
    french_runtime = french["runtime"]
    assert isinstance(english_runtime, dict)
    assert isinstance(french_runtime, dict)
    french_runtime[field] = english_runtime[field]
    _write_profile(tmp_path, "radiotedu-en.json", english)
    _write_profile(tmp_path, "radiotedu-fr.json", french)

    with pytest.raises(StationProfileError):
        load_station_profiles(tmp_path)
