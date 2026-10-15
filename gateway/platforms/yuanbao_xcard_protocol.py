"""xCard wire envelope over the existing elem_type 1013 custom message.

The IM protocol is not extended. A card, a client upevent and a bot downevent all travel as
``TIMCustomElem.data = {"elem_type": 1013, "text": "<json>"}`` where ``text`` is
``{"extend_type": 101|102|103, "content": ...}``. A 1013 whose text is ordinary markdown has no
extend_type and is left to the normal inbound pipeline.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

A2UI_VERSION = "v1.0"
CATALOG_ID = "https://yuanbao.tencent.com/specification/v1_0/catalogs/xcard/catalog.json"
EVENT_PROTOCOL = "yuanbao.openclaw.xcard/1"
EXTENSION_KEY = "com_tencent_yuanbao_openclaw"
MIN_RENDERER_VERSION_ERROR = "MIN_RENDERER_VERSION_NOT_SATISFIED"
ELEM_TYPE = 1013
EXT_CARD = 101
EXT_UP = 102
EXT_DOWN = 103
DOWN_PROTOCOL = "1.0"
# One IM packet is 12 KiB including framing; leave ~2 KiB of headroom.
MAX_CARD_CONTENT_BYTES = 10 * 1024


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _string(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _number(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _obj_value(value: Any) -> Optional[dict]:
    if _is_obj(value):
        return value
    if isinstance(value, str):
        parsed = _loads(value)
        return parsed if _is_obj(parsed) else None
    return None


def normalize_up_payload(payload: dict) -> dict:
    """Client snake_case (and the earlier camelCase draft) → one event shape."""
    if isinstance(payload.get("requestId"), str) or _is_obj(payload.get("event")):
        return payload
    event_data = _obj_value(payload.get("event_data")) or _obj_value(payload.get("action")) or {}
    context = (
        _obj_value(payload.get("context_json"))
        or _obj_value(payload.get("event_context"))
        or _obj_value(payload.get("action_context"))
        or _obj_value(payload.get("context"))
        or _obj_value(event_data.get("context"))
        or _obj_value(event_data.get("data"))
        or {}
    )
    request_id = _string(payload.get("event_id"), payload.get("request_id"))
    surface_id = _string(payload.get("surface_id"), payload.get("surfaceId"))
    event_name = _string(payload.get("event_name"), payload.get("action_name"))
    revision = _number(payload.get("revision"), payload.get("card_revision"), event_data.get("revision"))
    plugin_epoch = _string(payload.get("plugin_epoch"), event_data.get("plugin_epoch"))
    source_component = _string(payload.get("source_component_id"), event_data.get("sourceComponentId"))
    user_message = _string(payload.get("user_message"), event_data.get("userMessage"))
    trigger = _string(payload.get("trigger"), event_data.get("trigger"))
    timestamp = _string(payload.get("timestamp"), event_data.get("timestamp"))
    action: dict[str, Any] = {"context": context}
    if event_name:
        action["name"] = event_name
    if surface_id:
        action["surfaceId"] = surface_id
    if source_component:
        action["sourceComponentId"] = source_component
    if user_message:
        action["userMessage"] = user_message
    if trigger:
        action["trigger"] = trigger
    if timestamp:
        action["timestamp"] = timestamp
    binding: dict[str, Any] = {}
    if plugin_epoch:
        binding["pluginEpoch"] = plugin_epoch
    if revision is not None:
        binding["revision"] = revision
    if isinstance(payload.get("card_message_id"), str):
        binding["messageId"] = payload["card_message_id"]
    normalized: dict[str, Any] = {
        "protocol": EVENT_PROTOCOL,
        "binding": binding,
        "event": {
            "version": _string(payload.get("protocol_version"), event_data.get("version")) or A2UI_VERSION,
            "action": action,
        },
    }
    if request_id:
        normalized["requestId"] = request_id
    if surface_id:
        normalized["surfaceId"] = surface_id
    return normalized


def parse_up_event(msg_body: Any) -> Optional[dict]:
    """Upevent payload when ``msg_body`` is one 1013 carrier with extend_type 102, else None.

    The inbound proxy sometimes labels that carrier ``TIMTextElem`` while the JSON stays in
    ``msg_content.data``, so the element is identified by its data, not by msg_type.
    """
    if not isinstance(msg_body, list) or len(msg_body) != 1 or not isinstance(msg_body[0], dict):
        return None
    content = msg_body[0].get("msg_content") or {}
    data = content.get("data") if isinstance(content, dict) else None
    if not isinstance(data, str):
        return None
    outer = _loads(data)
    if not _is_obj(outer) or outer.get("elem_type") != ELEM_TYPE or not isinstance(outer.get("text"), str):
        return None
    envelope = _loads(outer["text"])
    if not _is_obj(envelope) or envelope.get("extend_type") != EXT_UP:
        return None
    raw = envelope.get("content")
    return normalize_up_payload(raw) if _is_obj(raw) else {}


def _elem(extend_type: int, content: Any) -> list[dict]:
    text = json.dumps({"extend_type": extend_type, "content": content}, ensure_ascii=False, separators=(",", ":"))
    data = json.dumps({"elem_type": ELEM_TYPE, "text": text}, ensure_ascii=False, separators=(",", ":"))
    return [{"msg_type": "TIMCustomElem", "msg_content": {"data": data}}]


def card_messages_body(messages: list) -> list[dict]:
    return _elem(EXT_CARD, messages)


def down_event_body(*, card_message_id: str, surface_id: str, revision: int, messages: list) -> list[dict]:
    return _elem(EXT_DOWN, {
        "protocol_version": DOWN_PROTOCOL,
        "delivery_id": str(uuid.uuid4()),
        "card_message_id": card_message_id,
        "surface_id": surface_id,
        "state_update": {"state_seq": revision, "operation": "snapshot", "content": messages},
    })


def receipt_body(*, card_message_id: str, surface_id: str, request_id: str, status: str, reason: str) -> list[dict]:
    ok = status in {"accepted", "completed"}
    return _elem(EXT_DOWN, {
        "protocol_version": DOWN_PROTOCOL,
        "delivery_id": str(uuid.uuid4()),
        "card_message_id": card_message_id,
        "surface_id": surface_id,
        "action_ack": {
            "event_id": request_id,
            "success": ok,
            "error_code": "" if ok else (reason or status),
            "error_message": "" if ok else (reason or status),
        },
    })


def text_body(text: str) -> list[dict]:
    return [{"msg_type": "TIMTextElem", "msg_content": {"text": text}}]


def wire_bytes(body: list) -> int:
    data = (body[0].get("msg_content") or {}).get("data") if body else ""
    return len(data.encode("utf-8")) if isinstance(data, str) else 0


def plain_text(msg_body: Any) -> str:
    """Joined TIMTextElem text, used to match a button userMessage echo."""
    parts: list[str] = []
    for elem in msg_body or []:
        if not isinstance(elem, dict) or elem.get("msg_type") != "TIMTextElem":
            continue
        content = elem.get("msg_content") or {}
        text = content.get("text") if isinstance(content, dict) else ""
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return " ".join(parts).strip()
