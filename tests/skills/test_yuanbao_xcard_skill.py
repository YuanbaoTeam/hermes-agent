"""The yuanbao-xcard skill validator accepts a minimal card and rejects a WebCard."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "optional-skills" / "yuanbao-xcard" / "scripts" / "validate_xcard.py"
CATALOG = "https://yuanbao.tencent.com/specification/v1_0/catalogs/xcard/catalog.json"


def _card(component: str = "Card") -> list[dict]:
    return [
        {"version": "v1.0", "createSurface": {"surfaceId": "s", "catalogId": CATALOG}},
        {
            "version": "v1.0",
            "updateComponents": {
                "surfaceId": "s",
                "components": [
                    {"id": "root", "component": component, "child": "label"},
                    {"id": "label", "component": "Text", "text": "Hi"},
                ],
            },
        },
    ]


def test_validator_accepts_minimal_card_and_rejects_webcard(tmp_path):
    good = tmp_path / "card.jsonl"
    good.write_text("\n".join(json.dumps(message) for message in _card()), encoding="utf-8")
    ok = subprocess.run([sys.executable, str(SCRIPT), str(good)], capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr

    bad = tmp_path / "web.jsonl"
    bad.write_text("\n".join(json.dumps(message) for message in _card("WebCard")), encoding="utf-8")
    rejected = subprocess.run([sys.executable, str(SCRIPT), str(bad)], capture_output=True, text=True, check=False)
    assert rejected.returncode == 1
    assert "WebCard" in rejected.stderr
