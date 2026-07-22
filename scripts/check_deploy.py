from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


DEFAULT_URL = "https://jarvis-web-cloud.onrender.com/status"
DEFAULT_APP = "Jarivs"
DEFAULT_VERSION = "1.7.2"


def fetch_json(url: str, timeout: int) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Status endpoint did not return a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the live Render Jarvis deployment.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Status endpoint to check.")
    parser.add_argument("--expect-app", default=DEFAULT_APP, help="Expected app name.")
    parser.add_argument("--expect-version", default=DEFAULT_VERSION, help="Expected app version.")
    parser.add_argument("--timeout", type=int, default=25, help="Request timeout in seconds.")
    args = parser.parse_args()

    try:
        status = fetch_json(args.url, args.timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        print(f"Deploy check failed: {exc}", file=sys.stderr)
        return 2

    app = str(status.get("app", ""))
    version = str(status.get("version", ""))
    state = str(status.get("status", ""))
    print(f"Live Render status: app={app} version={version} status={state}")

    failures = []
    if app != args.expect_app:
        failures.append(f"expected app {args.expect_app!r}, got {app!r}")
    if version != args.expect_version:
        failures.append(f"expected version {args.expect_version!r}, got {version!r}")
    if state not in {"ready", "degraded"}:
        failures.append(f"unexpected status {state!r}")

    if failures:
        print("Deploy check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("Deploy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
