from scripts.smoke_broadcast import _secret_isolation_contract, strict_failures


def complete_report() -> dict:
    station = {
        "music_library": {"track_count": 10},
        "coverage": {
            "planned_seconds": 14_400,
            "planned_required_seconds": 14_400,
            "rendered_seconds": 3_600,
            "rendered_required_seconds": 3_600,
            "fallback_seconds": 21_600,
            "fallback_required_seconds": 21_600,
        },
        "imaging": {"valid": True, "asset_count": 6, "all_jingles": True},
        "liquidsoap": {
            "rendered_contract": {"config_ok": True},
            "encoder_preflight": {"encoder_supported": True},
            "source_credentials_configured": True,
        },
        "tts": {"status": "ready"},
        "platform_api": {"status": 200},
    }
    return {
        "ollama": {"status": "ready"},
        "secret_isolation": {"ok": True},
        "public_sync": {"configured": True},
        "stations": {"radiotedu-en": station, "radiotedu-fr": station},
    }


def test_strict_smoke_accepts_complete_dual_station_contract() -> None:
    assert strict_failures(complete_report()) == []


def test_strict_smoke_reports_station_specific_jingle_and_fdk_failures() -> None:
    report = complete_report()
    report["stations"]["radiotedu-fr"] = {
        **report["stations"]["radiotedu-fr"],
        "imaging": {"valid": True, "asset_count": 5, "all_jingles": True},
        "liquidsoap": {
            **report["stations"]["radiotedu-fr"]["liquidsoap"],
            "encoder_preflight": {"encoder_supported": False},
        },
    }

    failures = strict_failures(report)

    assert "radiotedu-fr does not have all six validated jingles" in failures
    assert "radiotedu-fr Liquidsoap does not advertise FFmpeg encoding" in failures


def test_supervisor_scopes_source_credentials_and_removes_hmac_secrets() -> None:
    contract = _secret_isolation_contract()

    assert contract["ok"] is True
    assert set(contract["station_children"]) == {"radiotedu-en", "radiotedu-fr"}
