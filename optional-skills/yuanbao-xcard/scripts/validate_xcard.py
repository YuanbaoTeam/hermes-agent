#!/usr/bin/env python3
"""Static check for a self-contained xCard JSONL file. Standard library only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_BYTES = 10 * 1024
CATALOG = "https://yuanbao.tencent.com/specification/v1_0/catalogs/xcard/catalog.json"
BANNED_COMPONENTS = {"WebCard", "xCardTemplate"}


def _load(path: Path) -> list:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("file is empty")
    if text.startswith("["):
        parsed = json.loads(text)
    else:
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("expected a JSON array or JSONL objects")
    return parsed


def _walk(value, at: str, errors: list[str]) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{at}[{index}]", errors)
        return
    if not isinstance(value, dict):
        return
    component = value.get("component")
    if component in BANNED_COMPONENTS:
        errors.append(f"{at}.component: {component} is not self-contained")
    if "tracking" in value:
        errors.append(f"{at}.tracking: not allowed")
    call = value.get("call")
    if isinstance(call, str) and call.startswith("@hostEnv"):
        errors.append(f"{at}.call: @hostEnv is not allowed")
    for key, child in value.items():
        _walk(child, f"{at}.{key}", errors)


def validate(messages: list) -> list[str]:
    errors: list[str] = []
    if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        errors.append(f"card exceeds {MAX_BYTES} bytes")
    surface = None
    ids: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("version") != "v1.0":
            errors.append(f"messages[{index}] must be a v1.0 object")
            continue
        kinds = [key for key in ("createSurface", "updateComponents", "updateDataModel") if key in message]
        if len(kinds) != 1:
            errors.append(f"messages[{index}] must contain one surface message")
            continue
        body = message[kinds[0]]
        if not isinstance(body, dict):
            errors.append(f"messages[{index}].{kinds[0]} must be an object")
            continue
        sid = body.get("surfaceId")
        if not isinstance(sid, str) or not sid:
            errors.append(f"messages[{index}] missing surfaceId")
        elif surface is None:
            surface = sid
        elif sid != surface:
            errors.append(f"messages[{index}] surfaceId mismatch")
        if kinds[0] == "createSurface" and body.get("catalogId") != CATALOG:
            errors.append(f"messages[{index}].catalogId must be the xcard catalog")
        components = body.get("components")
        if isinstance(components, list):
            for component in components:
                if isinstance(component, dict) and isinstance(component.get("id"), str):
                    ids.add(component["id"])
    if "root" not in ids:
        errors.append('missing component id "root"')
    _walk(messages, "messages", errors)
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_xcard.py CARD.jsonl", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        messages = _load(path)
        errors = validate(messages)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"ok {path} ({len(messages)} messages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
