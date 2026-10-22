"""Structural checks for an AI-authored A2UI v1.0 Inline Surface.

This is the runtime subset of the xcard-generator validator: envelope, one surface, known
components, resolvable child ids, no agent-RPC, and the self-contained ban (WebCard,
xCardTemplate, tracking, @hostEnv, undeclared host functions). Property-level catalog checks
stay with the skill's script and with the client renderer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from gateway.platforms.yuanbao_xcard_protocol import (
    A2UI_VERSION,
    CATALOG_ID,
    EXTENSION_KEY,
    MAX_CARD_CONTENT_BYTES,
)

COMPONENTS = frozenset({
    "AudioPlayer", "Box", "Button", "Card", "CheckBox", "ChoiceGroup", "ChoicePicker", "Clickable",
    "Column", "ConditionView", "DateTimeInput", "Divider", "DotIndicator", "FlowColumn", "FlowRow",
    "HorizontalGrid", "Icon", "IconButton", "Image", "ItemPicker", "Label", "List", "Modal", "Pager",
    "PlatformSwitch", "RichText", "Row", "Slider", "Stepper", "SvgView", "Switcher", "Tabs", "Text",
    "TextField", "VerticalGrid", "Video", "Visibility", "WebCard", "xCardTemplate",
})
KNOWN_FUNCTIONS = frozenset({
    "add", "and", "closeModal", "contains", "copyContent", "email", "equalsNumber", "equalsString",
    "filter", "formatCurrency", "formatDate", "formatNumber", "formatString", "greaterThan",
    "greaterThanOrEqual", "isEmpty", "join", "length", "lessThan", "lessThanOrEqual", "map", "multiply",
    "not", "numeric", "openHostRoute", "openUrl", "openWechatMiniProgram", "or", "pluralize",
    "publishDataModel", "regex", "rendererVersionAtLeast", "repeatQuery", "required", "select",
    "sendQuery", "sequence", "setTimeout", "subtract", "switch", "toast", "updateData", "urlEncode",
    "yb_chat_repeat", "yb_chat_send_prompt", "yb_navigation_open_host_route", "yb_navigation_open_url",
})
FORBIDDEN_KEYS = frozenset({"callAgentFunction", "agentFunctionResponse"})
MESSAGE_KINDS = ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")


@dataclass
class ValidateResult:
    ok: bool
    errors: list[str]
    warnings: list[str]


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def parse_generated_messages(raw: Any) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        return {"error": "messages must be a non-empty JSONL string or JSON array"}
    try:
        text = raw.strip()
        if text.startswith("["):
            parsed = json.loads(text)
        else:
            parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        return {"error": f"messages is not valid JSONL or a JSON array: {exc.msg}"}
    if not isinstance(parsed, list) or not parsed or not all(_is_obj(item) for item in parsed):
        return {"error": "messages must contain at least one JSON object"}
    return {"messages": parsed}


def _kind(message: dict) -> Optional[str]:
    found = [name for name in MESSAGE_KINDS if name in message]
    return found[0] if len(found) == 1 else None


def _referenced_ids(component: dict) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            found.append(value)

    add(component.get("child"))
    children = component.get("children")
    if isinstance(children, list):
        for child in children:
            add(child)
    elif _is_obj(children):
        add(children.get("componentId"))
    name = component.get("component")
    if name == "Modal":
        add(component.get("trigger"))
        add(component.get("content"))
    elif name in {"Switcher"}:
        add(component.get("content"))
    elif name == "Tabs" and isinstance(component.get("tabs"), list):
        for tab in component["tabs"]:
            if _is_obj(tab):
                add(tab.get("child"))
    elif name == "ConditionView":
        if isinstance(component.get("cases"), list):
            for case in component["cases"]:
                if _is_obj(case):
                    add(case.get("child"))
        add(component.get("default"))
    elif name == "PlatformSwitch" and _is_obj(component.get("condition")):
        for child in component["condition"].values():
            add(child)
    elif name == "ChoiceGroup" and _is_obj(component.get("options")):
        add(component["options"].get("componentId"))
    return found


def _scan_calls(value: Any, at: str, errors: list[str], warnings: list[str]) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_calls(item, f"{at}[{index}]", errors, warnings)
        return
    if not _is_obj(value):
        return
    for key, child in value.items():
        if key in FORBIDDEN_KEYS:
            errors.append(f"{at}.{key}: agent function calls are not supported")
        if key == "call" and isinstance(child, str) and not child.startswith("@") and child not in KNOWN_FUNCTIONS:
            warnings.append(f"{at}.call: function {child} is not in the official catalog")
        _scan_calls(child, f"{at}.{key}", errors, warnings)


def _scan_forbidden(value: Any, at: str, errors: list[str]) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_forbidden(item, f"{at}[{index}]", errors)
        return
    if not _is_obj(value):
        return
    component = value.get("component")
    if component in {"WebCard", "xCardTemplate"}:
        errors.append(f"{at}.component: self-contained cards cannot use {component}")
    for key, child in value.items():
        if key == "tracking":
            errors.append(f"{at}.tracking: host tracking is not enabled")
        if key == "call" and isinstance(child, str) and child.startswith("@hostEnv"):
            errors.append(f"{at}.call: host environment variables are not allowed")
        _scan_forbidden(child, f"{at}.{key}", errors)


def _collect_components(items: list, at: str, errors: list[str], components: dict[str, dict]) -> None:
    for index, raw in enumerate(items):
        if not _is_obj(raw):
            errors.append(f"{at}[{index}] must be an object")
            continue
        if not isinstance(raw.get("id"), str) or not raw["id"]:
            errors.append(f"{at}[{index}].id is required")
        if not isinstance(raw.get("component"), str):
            errors.append(f"{at}[{index}].component must be a component name")
        elif raw["component"] not in COMPONENTS:
            errors.append(f"{at}[{index}].component unknown component {raw['component']}")
        if isinstance(raw.get("id"), str):
            components[raw["id"]] = raw


def _align_surface(body: dict, kind: str, at: str, surface_id: Optional[str], errors: list[str]) -> Optional[str]:
    sid = body.get("surfaceId")
    if not isinstance(sid, str) or not sid:
        errors.append(f"{at}.{kind}.surfaceId is required")
        return surface_id
    if surface_id is None:
        return sid
    if sid != surface_id:
        errors.append(f"{at}.{kind}.surfaceId does not match the card's other messages")
    return surface_id


def _check_message_kind(kind: str, body: dict, index: int, at: str, errors: list[str], components: dict) -> None:
    if kind == "createSurface":
        if index != 0:
            errors.append(f"{at}: createSurface must be the first message")
        if body.get("catalogId") != CATALOG_ID:
            errors.append(f"{at}.createSurface.catalogId must be the xcard catalog")
        if isinstance(body.get("components"), list):
            _collect_components(body["components"], f"{at}.createSurface.components", errors, components)
        return
    if kind == "updateComponents":
        if not isinstance(body.get("components"), list):
            errors.append(f"{at}.updateComponents.components must be an array")
        else:
            _collect_components(body["components"], f"{at}.updateComponents.components", errors, components)
        return
    if kind == "updateDataModel" and "value" not in body:
        errors.append(f"{at}.updateDataModel.value is required")
    elif kind == "deleteSurface":
        errors.append(f"{at}: deleteSurface is not supported; close the card instead")


def _check_message(index: int, message: Any, surface_id: Optional[str], errors: list[str], components: dict) -> Optional[str]:
    at = f"messages[{index}]"
    if not _is_obj(message):
        errors.append(f"{at} must be an object")
        return surface_id
    if message.get("version") != A2UI_VERSION:
        errors.append(f'{at}.version must be "{A2UI_VERSION}"')
    kind = _kind(message)
    if kind is None:
        errors.append(f"{at} must contain exactly one of {', '.join(MESSAGE_KINDS)}")
        return surface_id
    body = message[kind]
    if not _is_obj(body):
        errors.append(f"{at}.{kind} must be an object")
        return surface_id
    surface_id = _align_surface(body, kind, at, surface_id, errors)
    _check_message_kind(kind, body, index, at, errors, components)
    return surface_id


def _check_component_refs(components: dict[str, dict], errors: list[str]) -> None:
    if "root" not in components:
        errors.append('the final component set is missing id "root"')
    for component in components.values():
        for ref in _referenced_ids(component):
            if ref not in components:
                errors.append(f"component {component.get('id')} references missing {ref}")


def validate_a2ui_messages(messages: list, *, max_bytes: int = MAX_CARD_CONTENT_BYTES) -> ValidateResult:
    errors: list[str] = []
    warnings: list[str] = []
    components: dict[str, dict] = {}
    surface_id: Optional[str] = None
    for index, message in enumerate(messages):
        surface_id = _check_message(index, message, surface_id, errors, components)
    if not messages or not _is_obj(messages[0]) or _kind(messages[0]) != "createSurface":
        errors.append("messages[0] must be createSurface")
    _check_component_refs(components, errors)
    _scan_calls(messages, "messages", errors, warnings)
    size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    if size > max_bytes:
        errors.append(f"card content is {size} bytes, over the {max_bytes} byte limit")
    return ValidateResult(ok=not errors, errors=errors, warnings=warnings)


def validate_generated_messages(messages: list) -> ValidateResult:
    base = validate_a2ui_messages(messages)
    errors = list(base.errors)
    _scan_forbidden(messages, "messages", errors)
    errors.extend(f"{warning} (free-form send disallows unregistered host functions)" for warning in base.warnings)
    return ValidateResult(ok=not errors, errors=errors, warnings=[])


def extract_actions(messages: list) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not _is_obj(value):
            return
        event = value.get("event")
        if _is_obj(event) and isinstance(event.get("name"), str) and event["name"].strip():
            name = event["name"].strip()
            if name not in seen:
                seen.add(name)
                names.append(name)
        for child in value.values():
            visit(child)

    for message in messages:
        update = message.get("updateComponents") if _is_obj(message) else None
        if _is_obj(update) and isinstance(update.get("components"), list):
            visit(update["components"])
    return names


def _result_text(state: str, last_result: Optional[dict]) -> str:
    context = (last_result or {}).get("context") or {}
    if isinstance(context, dict) and isinstance(context.get("resultText"), str) and context["resultText"].strip():
        return context["resultText"].strip()
    user_message = (last_result or {}).get("userMessage") or ""
    if isinstance(user_message, str) and user_message.strip():
        return user_message.strip()
    summary = (last_result or {}).get("summary") or ""
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return "" if state == "active" else "操作已完成"


def render_generated_messages(
    source: list,
    surface_id: str,
    extension: dict,
    *,
    state: Optional[str] = None,
    revision: Optional[int] = None,
    last_result: Optional[dict] = None,
) -> list:
    """Copy messages, force the plugin surface id, and project ``/__openclaw`` when rendering."""
    messages = json.loads(json.dumps(source))
    for message in messages:
        for kind in ("createSurface", "updateComponents", "updateDataModel"):
            body = message.get(kind)
            if _is_obj(body):
                body["surfaceId"] = surface_id
        create = message.get("createSurface")
        if not _is_obj(create):
            continue
        metadata = create.get("metadata") if _is_obj(create.get("metadata")) else {}
        extensions = metadata.get("extensions") if _is_obj(metadata.get("extensions")) else {}
        create["metadata"] = {**metadata, "extensions": {**extensions, EXTENSION_KEY: extension}}
    if state is None:
        return messages
    if state != "active":
        for message in messages:
            update = message.get("updateComponents")
            if not _is_obj(update) or not isinstance(update.get("components"), list):
                continue
            for component in update["components"]:
                if _is_obj(component) and component.get("id") == "root":
                    component["enabled"] = False
    messages.append({
        "version": A2UI_VERSION,
        "updateDataModel": {
            "surfaceId": surface_id,
            "path": "/__openclaw",
            "value": {
                "state": state,
                "completed": state != "active",
                "revision": revision if revision is not None else 0,
                "action": (last_result or {}).get("action") or "",
                "context": (last_result or {}).get("context") or {},
                "resultText": _result_text(state, last_result),
            },
        },
    })
    return messages
