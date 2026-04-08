#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = os.getenv("BOSS_BASE_URL", "http://127.0.0.1:8321")
RUN_CHAT = os.getenv("BOSS_SMOKE_CHAT", "0").strip().lower() in {"1", "true", "yes", "on"}
CHAT_MESSAGE = os.getenv("BOSS_SMOKE_CHAT_MESSAGE", "Reply with the single word ok.")


def main() -> int:
    status = get_json("/api/system/status")
    print(f"PASS /api/system/status provider_mode={status.get('provider_mode')} pid={status.get('process_id')}")

    memory_stats = get_json("/api/memory/stats")
    print(
        "PASS /api/memory/stats "
        f"projects={memory_stats.get('projects')} files_indexed={memory_stats.get('files_indexed')}"
    )

    if RUN_CHAT:
        final_text = run_chat_roundtrip(CHAT_MESSAGE)
        print(f"PASS /api/chat text={final_text!r}")
    else:
        print("SKIP /api/chat roundtrip (set BOSS_SMOKE_CHAT=1 to enable)")

    print("Boss local smoke passed.")
    return 0


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=5) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def run_chat_roundtrip(message: str) -> str:
    payload = json.dumps(
        {
            "message": message,
            "session_id": f"smoke-local-{int(time.time())}",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    text_fragments: list[str] = []
    error_message: str | None = None
    event_type: str | None = None
    data_line: str | None = None

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"/api/chat returned HTTP {response.status}")

        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith("event: "):
                event_type = line[7:]
                continue
            if line.startswith("data: "):
                data_line = line[6:]
                continue
            if line:
                continue

            if not data_line:
                event_type = None
                continue

            payload = json.loads(data_line)
            payload_type = payload.get("type") or event_type
            if payload_type == "text":
                content = payload.get("content")
                if isinstance(content, str):
                    text_fragments.append(content)
            elif payload_type == "error":
                error_message = str(payload.get("message") or "Unknown chat error")
            elif payload_type == "done":
                break

            event_type = None
            data_line = None

    if error_message:
        raise RuntimeError(error_message)

    final_text = "".join(text_fragments).strip()
    if not final_text:
        raise RuntimeError("/api/chat completed without any assistant text")
    return final_text


if __name__ == "__main__":
    raise SystemExit(main())