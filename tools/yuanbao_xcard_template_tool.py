"""Yuanbao xCard template tool: built-in template cards in the current group.

One tool, six actions: list_templates / get_template / create / update / close / get.
The model supplies template data only; components are built by
``gateway.platforms.yuanbao_xcard_templates``. Same gate as the free-form tools:
``platforms.yuanbao.extra.xcard_enabled: true`` and a group session.
"""

from __future__ import annotations

import logging

from gateway.session_context import get_session_env
from tools.registry import registry, tool_result
from tools.yuanbao_tools import _YbError, _check_yuanbao

# Helpers are imported lazily: yuanbao_tools imports this module for its registration
# side effect while tools.yuanbao_xcard_tools may still be mid-import.
logger = logging.getLogger(__name__)


def _describe(rec) -> dict:
    out = {
        "surfaceId": rec.surface_id,
        "template": rec.template,
        "title": rec.title,
        "state": rec.state,
        "revision": rec.revision,
        "audience": rec.audience,
    }
    if rec.last_error:
        out["rendererError"] = {"code": rec.last_error.get("code"), "message": rec.last_error.get("message")}
    if rec.history:
        out["recentActions"] = [
            {"action": item.get("action"), "summary": item.get("summary"), "context": item.get("context"),
             "operatorUserId": item.get("operatorUserId")}
            for item in rec.history
        ]
    return out


def _unknown_template(key) -> str:
    from gateway.platforms.yuanbao_xcard_templates import list_templates

    return tool_result({
        "ok": False,
        "error": f"未知模板: {key}" if key else "请指定 template",
        "templates": list_templates(),
        "note": "从 templates 中选一个 key，先用 action=get_template 查看字段要求。",
    })


def _data_failure(tpl, errors, warnings) -> str:
    return tool_result({
        "ok": False,
        "error": "data 校验失败",
        "errors": [f"{e['path']}: {e['message']}" if e.get("path") else e["message"] for e in errors[:20]],
        **({"warnings": warnings[:10]} if warnings else {}),
        "retryable": True,
        "note": "按 errors 修正 data 后重试；字段要求见 fields，可参考 example。",
        "fields": tpl.fields,
        "example": tpl.example,
    })


def _find_own(adapter, surface_id: str, group_code: str):
    from gateway.platforms.yuanbao_xcard_registry import get_surface

    rec = get_surface(adapter, surface_id) if surface_id else None
    if rec is None or rec.kind != "template" or rec.group_code != group_code:
        return None
    return rec


async def yb_xcard_template(args: dict, **_kw) -> str:
    from tools.yuanbao_xcard_tools import _adapter, _enabled, _failure, _group_code, _refuse

    from gateway.platforms.yuanbao_xcard_template_schema import parse_template_data
    from gateway.platforms.yuanbao_xcard_templates import (
        describe_template,
        get_template,
        list_templates,
        prepare_template_data,
    )

    action = args.get("action")
    if action == "list_templates":
        return tool_result({"ok": True, "templates": list_templates()})
    if action == "get_template":
        tpl = get_template(args.get("template") or "")
        if tpl is None:
            return _unknown_template(args.get("template"))
        return tool_result({"ok": True, **describe_template(tpl), "note": "create 时 data 传按 fields 填写的 JSON 对象文本。"})

    group_code = _group_code()
    if not group_code:
        return _refuse("xCard can only be sent in a Yuanbao group")
    try:
        adapter = _adapter()
    except _YbError as exc:
        return _refuse(str(exc))
    if not _enabled(adapter):
        return _refuse("xCard is disabled. Set platforms.yuanbao.extra.xcard_enabled: true and restart the gateway.")
    if action not in {"create", "update", "close", "get"}:
        return _refuse(f"不支持的 action: {action}。可选 list_templates/get_template/create/update/close/get", retryable=True)

    from tools.yuanbao_xcard_tools import wait_extra

    try:
        if action == "create":
            return await _create(adapter, args, group_code, wait_extra)
        if action == "update":
            return await _update(adapter, args, group_code, wait_extra)
        if action == "close":
            return await _close(adapter, args, group_code)
        return _get(adapter, args, group_code)
    except Exception as exc:
        from gateway.platforms.yuanbao_xcard_service import XCardSendError

        if isinstance(exc, XCardSendError):
            return _failure(exc)
        logger.exception("[xcard] template tool failed action=%s", action)
        return _failure(exc)


async def _create(adapter, args, group_code, wait_extra) -> str:
    from tools.yuanbao_xcard_tools import _on_gateway, _refuse

    from gateway.platforms.yuanbao_xcard_service import create_template_card
    from gateway.platforms.yuanbao_xcard_template_schema import parse_template_data
    from gateway.platforms.yuanbao_xcard_templates import get_template, prepare_template_data

    tpl = get_template(args.get("template") or "")
    if tpl is None:
        return _unknown_template(args.get("template"))
    parsed = parse_template_data(args.get("data"))
    if "error" in parsed:
        return tool_result({"ok": False, "error": parsed["error"], "retryable": True,
                            "fields": tpl.fields, "example": tpl.example})
    prepared = prepare_template_data(tpl, parsed["data"])
    if not prepared["ok"]:
        return _data_failure(tpl, prepared["errors"], prepared["warnings"])
    audience = "anyone" if args.get("audience") == "anyone" else "requester"
    owner = get_session_env("HERMES_SESSION_USER_ID", "")
    if audience == "requester" and not owner:
        return _refuse("无法确定发起用户，请使用 audience=anyone")

    async def _send():
        return await create_template_card(
            adapter, group_code=group_code,
            session_key=get_session_env("HERMES_SESSION_KEY", ""),
            owner_user_id=owner, audience=audience, tpl=tpl, data=prepared["data"],
        )

    rec = await _on_gateway(adapter, _send())
    extra = {"warnings": prepared["warnings"]} if prepared["warnings"] else {}
    waited = await wait_extra(adapter, rec, group_code, args)
    return tool_result({"ok": True, **_describe(rec), **extra, **waited})


async def _update(adapter, args, group_code, wait_extra) -> str:
    from tools.yuanbao_xcard_tools import _on_gateway, _refuse

    from gateway.platforms.yuanbao_xcard_registry import lock_for
    from gateway.platforms.yuanbao_xcard_service import update_template_card
    from gateway.platforms.yuanbao_xcard_template_schema import parse_template_data
    from gateway.platforms.yuanbao_xcard_templates import get_template, prepare_template_data

    surface_id = args.get("surfaceId") if isinstance(args.get("surfaceId"), str) else ""

    async def _run():
        rec = _find_own(adapter, surface_id.strip(), group_code)
        if rec is None:
            return _refuse("卡片不存在或不属于当前群")
        tpl = get_template(rec.template)
        if tpl is None:
            return _refuse(f"卡片模板已不存在: {rec.template}")
        parsed = parse_template_data(args.get("data"))
        if "error" in parsed:
            return tool_result({"ok": False, "error": parsed["error"], "retryable": True})
        # Shallow merge: top-level fields in data replace the card's current values.
        prepared = prepare_template_data(tpl, {**rec.data, **parsed["data"]})
        if not prepared["ok"]:
            return _data_failure(tpl, prepared["errors"], prepared["warnings"])
        async with lock_for(adapter, rec.surface_id):
            if rec.state != "active":
                return _refuse(f"卡片已是 {rec.state} 状态，不能更新")
            await update_template_card(adapter, rec, tpl, prepared["data"])
        waited = await wait_extra(adapter, rec, group_code, args)
        extra = {"warnings": prepared["warnings"]} if prepared["warnings"] else {}
        return tool_result({"ok": True, **_describe(rec), **extra, **waited})

    return await _on_gateway(adapter, _run())


async def _close(adapter, args, group_code) -> str:
    from tools.yuanbao_xcard_tools import _on_gateway, _refuse

    from gateway.platforms.yuanbao_xcard_registry import lock_for
    from gateway.platforms.yuanbao_xcard_service import push_surface_update

    surface_id = args.get("surfaceId") if isinstance(args.get("surfaceId"), str) else ""

    async def _run():
        rec = _find_own(adapter, surface_id.strip(), group_code)
        if rec is None:
            return _refuse("卡片不存在或不属于当前群")
        warning = ""
        async with lock_for(adapter, rec.surface_id):
            if rec.state == "active":
                rec.state = "completed" if rec.last_result else "cancelled"
                try:
                    await push_surface_update(adapter, rec)
                except Exception as exc:
                    logger.exception("[xcard] close refresh failed surface=%s", rec.surface_id)
                    warning = f"卡片已关闭，但界面刷新失败: {exc}"
        return tool_result({"ok": True, **_describe(rec), **({"warning": warning} if warning else {})})

    return await _on_gateway(adapter, _run())


def _get(adapter, args, group_code) -> str:
    from tools.yuanbao_xcard_tools import _refuse

    from gateway.platforms.yuanbao_xcard_registry import list_surfaces

    surface_id = args.get("surfaceId") if isinstance(args.get("surfaceId"), str) else ""
    if surface_id.strip():
        rec = _find_own(adapter, surface_id.strip(), group_code)
        if rec is None:
            return _refuse("卡片不存在或已过期")
        return tool_result({"ok": True, **_describe(rec)})
    cards = [rec for rec in list_surfaces(adapter, group_code) if rec.kind == "template"]
    return tool_result({"ok": True, "cards": [_describe(rec) for rec in cards]})


_SCHEMA = {
    "name": "yb_xcard_template",
    "description": (
        "Send and manage a built-in template xCard in the current Yuanbao group. "
        "Use action=list_templates first, then action=get_template to see a template's fields, events and example, "
        "then action=create with data as a JSON object string filled per those fields. "
        "Prefer this over yb_xcard_send for a standard choice, form, item list, confirmation or info card; "
        "use yb_xcard_send only when no template fits. "
        "action=update replaces top-level data fields on an active card, action=close closes it, action=get reads state. "
        "Set waitForAction only when this turn cannot continue without the click."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_templates", "get_template", "create", "update", "close", "get"],
                "description": "list_templates=列出模板；get_template=查看模板字段；create=发送新卡片；update=更新卡片；close=关闭卡片；get=查询卡片状态。",
            },
            "template": {"type": "string", "description": "Template key from list_templates (required for get_template/create)."},
            "data": {"type": "string", "description": "For create/update: the card data as a JSON object string, filled per get_template's fields."},
            "surfaceId": {"type": "string", "description": "For update/close/get: the surfaceId returned by create."},
            "audience": {
                "type": "string",
                "enum": ["requester", "anyone"],
                "description": "requester (default) or anyone in the group.",
            },
            "waitForAction": {"type": "boolean", "description": "Block until the user clicks or the wait times out."},
            "waitTimeoutSec": {"type": "number", "description": "Wait timeout in seconds, default 120, max 300."},
        },
        "required": ["action"],
    },
}

registry.register(
    name="yb_xcard_template",
    toolset="hermes-yuanbao",
    schema=_SCHEMA,
    handler=yb_xcard_template,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="🧩",
)
