"""Turn a template + validated data into A2UI v1.0 messages, plus component helpers.

Every template card has the same outer frame (component ids prefixed ``oc-``, never used by
templates): root Card → Column[body, result line]. ``body.enabled`` is bound to
``not(/ui/closed)`` so closing a card disables every control in it, and the result line
appears below. Only property shapes already accepted by the official validator are used
(Text variants h4/h5/body/caption, Button variants primary/default, color tokens
``basic_text_*``), plus ``style.fillMaxWidth`` on the root Card.
"""

from __future__ import annotations

from typing import Any

from gateway.platforms.yuanbao_xcard_protocol import A2UI_VERSION, CATALOG_ID, EXTENSION_KEY

BODY_ID = "body"


def path(p: str) -> dict:
    return {"path": p}


def not_path(p: str) -> dict:
    return {"call": "not", "args": {"value": {"path": p}}}


def format_string(value: str) -> dict:
    return {"call": "formatString", "args": {"value": value}}


def text(cid: str, value: Any, variant: str = "body", color: str = "") -> dict:
    out = {"id": cid, "component": "Text", "text": value, "variant": variant}
    if color:
        out["style"] = {"color": color}
    return out


def column(cid: str, children: list, spacing: int = 8) -> dict:
    return {"id": cid, "component": "Column", "children": children, "align": "stretch", "style": {"spacing": spacing}}


def button(cid: str, label: Any, action: dict, variant: str = "default") -> list:
    return [
        {"id": cid, "component": "Button", "child": f"{cid}-label", "variant": variant, "action": action},
        {"id": f"{cid}-label", "component": "Text", "text": label},
    ]


def event(name: str, context: dict | None = None, user_message: Any = None) -> dict:
    out: dict[str, Any] = {"event": {"name": name, "context": context or {}}}
    if user_message is not None:
        out["event"]["userMessage"] = user_message
    return out


def open_url(url: str) -> dict:
    return {"functionCall": {"call": "openUrl", "args": {"url": url}}}


def visible_if(cid: str, flag_path: str, child: str) -> dict:
    return {"id": cid, "component": "Visibility", "value": {"path": flag_path}, "child": child}


def build_template_messages(tpl, data: dict, *, closed: bool, last: dict | None,
                            surface_id: str, extension: dict) -> list:
    """``tpl`` is a ``yuanbao_xcard_templates.XCardTemplate``; ``last`` is the last accepted result."""
    state = {"closed": closed, "last": last}
    body = [
        {**component, "enabled": not_path("/ui/closed")} if component.get("id") == BODY_ID else component
        for component in tpl.components(data)
    ]
    components = [
        {"id": "root", "component": "Card", "child": "oc-frame", "style": {"padding": 12, "fillMaxWidth": 1}},
        column("oc-frame", [BODY_ID, "oc-result"]),
        *body,
        visible_if("oc-result", "/ui/hasResult", "oc-result-text"),
        text("oc-result-text", path("/ui/resultText"), "caption", "basic_text_tertiary"),
    ]
    result_text = (tpl.result_text(data, state) or "卡片已关闭") if closed else ""
    model = {**tpl.data_model(data, state), "ui": {"closed": closed, "hasResult": bool(result_text), "resultText": result_text}}
    return [
        {"version": A2UI_VERSION, "createSurface": {
            "surfaceId": surface_id, "catalogId": CATALOG_ID,
            "metadata": {"extensions": {EXTENSION_KEY: extension}}}},
        {"version": A2UI_VERSION, "updateComponents": {"surfaceId": surface_id, "components": components}},
        {"version": A2UI_VERSION, "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": model}},
    ]
