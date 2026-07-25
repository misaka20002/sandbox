from __future__ import annotations

import argparse
import hashlib
import json
import os

import httpx


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing environment variable: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the private sandbox API")
    parser.add_argument("command")
    parser.add_argument("--session")
    parser.add_argument("--replace-session")
    parser.add_argument("--owner", default="robot-demo")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--python-package", action="append", default=[])
    parser.add_argument("--node-package", action="append", default=[])
    arguments = parser.parse_args()

    api_url = required_environment("SANDBOX_API_URL").rstrip("/")
    sandbox_token = required_environment("SANDBOX_TOKEN")
    owner_key = hashlib.sha256(arguments.owner.encode("utf-8")).hexdigest()
    new_session = not arguments.session
    if arguments.replace_session:
        new_session = True
    response = httpx.post(
        f"{api_url}/v1/exec",
        headers={
            "Authorization": f"Bearer {sandbox_token}",
        },
        json={
            "session_id": arguments.session,
            "new_session": new_session,
            "replace_session_id": arguments.replace_session,
            "owner_key": owner_key,
            "command": arguments.command,
            "timeout_seconds": arguments.timeout,
            "python_packages": arguments.python_package,
            "node_packages": arguments.node_package,
        },
        timeout=arguments.timeout + 30,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
