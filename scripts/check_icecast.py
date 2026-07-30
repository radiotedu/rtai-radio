from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def check_icecast(url: str, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "RadioTEDU-Icecast-Check/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "reachable": True,
                "mount_active": response.status in {200, 206},
                "status": response.status,
                "url": url,
            }
    except urllib.error.HTTPError as exc:
        return {
            "reachable": True,
            "mount_active": False,
            "status": exc.code,
            "url": url,
            "error": str(exc),
        }
    except OSError as exc:
        return {
            "reachable": False,
            "mount_active": False,
            "status": None,
            "url": url,
            "error": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the canonical RadioTEDU Icecast mounts.")
    parser.add_argument("--host", default="10.98.98.75")
    parser.add_argument("--port", type=int, default=11154)
    parser.add_argument("--mount", action="append", default=[])
    parser.add_argument("--url", default="")
    args = parser.parse_args()

    if args.url:
        results = [check_icecast(args.url)]
    else:
        mounts = args.mount or ["/ai", "/event"]
        normalized = [mount if mount.startswith("/") else f"/{mount}" for mount in mounts]
        results = [check_icecast(f"http://{args.host}:{args.port}{mount}") for mount in normalized]
    print(json.dumps({"host": args.host, "port": args.port, "mounts": results}, ensure_ascii=True))
    return 0 if all(result["reachable"] and result["mount_active"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
