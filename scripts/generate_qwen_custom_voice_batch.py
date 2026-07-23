"""Render a trusted JSONL batch with the local Qwen CustomVoice model."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


MODEL = Path(
    r"C:\Program Files\RadioTEDU Broadcast Wall\_internal\models"
    r"\qwen3-tts-0.6b-customvoice"
)


def temporary_broadcast_is_live() -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'run_temporary_station.py' } | "
                "Select-Object -First 1 -ExpandProperty ProcessId"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return bool(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requests", type=Path)
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--speaker", default="Ryan")
    parser.add_argument(
        "--allow-while-live",
        action="store_true",
        help="Explicitly accept CPU contention with a live temporary broadcast.",
    )
    args = parser.parse_args()

    if os.name == "nt":
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)

    if temporary_broadcast_is_live() and not args.allow_while_live:
        raise SystemExit(
            "Refusing to load Qwen while the temporary broadcast is live; "
            "pre-render off-air or use --allow-while-live explicitly."
        )

    import soundfile
    import torch
    from qwen_tts import Qwen3TTSModel

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    requests = [
        json.loads(line)
        for line in args.requests.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    print(json.dumps({"event": "loading", "requests": len(requests)}), flush=True)
    started = time.monotonic()
    model = Qwen3TTSModel.from_pretrained(
        str(args.model.resolve(strict=True)),
        device_map="cpu",
        dtype=torch.float32,
    )
    print(
        json.dumps(
            {"event": "ready", "load_seconds": round(time.monotonic() - started, 2)}
        ),
        flush=True,
    )
    for request in requests:
        output = Path(request["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        item_started = time.monotonic()
        waveforms, sample_rate = model.generate_custom_voice(
            text=request["text"],
            speaker=str(request.get("speaker") or args.speaker),
            language=request["language"],
            instruct=request.get("instruct"),
        )
        temporary = output.with_suffix(".partial.wav")
        soundfile.write(temporary, waveforms[0], sample_rate, subtype="PCM_16")
        temporary.replace(output)
        print(
            json.dumps(
                {
                    "event": "rendered",
                    "output_path": str(output),
                    "sample_rate": int(sample_rate),
                    "synthesis_seconds": round(time.monotonic() - item_started, 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
