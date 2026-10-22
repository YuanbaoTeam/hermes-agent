"""Yuanbao xCard tools: send and update a self-contained A2UI card in the current group.

The tools are part of the yuanbao toolset. They no-op with a structured error unless the
connected adapter was started with ``platforms.yuanbao.extra.xcard_enabled: true`` and the
session is a group. Private chats cannot render the card.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.session_context import get_session_env
from tools.registry import registry, tool_result
from tools.yuanbao_tools import _YbError, _check_yuanbao

logger = logging.getLogger(__name__)

_DEFAULT_WAIT = 120
_MAX_WAIT = 300


def _adapter():
    from tools.yuanbao_tools import _adapter as active
    return active()


def _group_code() -> str:
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if get_session_env("HERMES_SESSION_CHAT_TYPE", "") != "group" or not chat_id.startswith("group:"):
        return ""
    return chat_id[len("group:"):]


def _enabled(adapter) -> bool:
    return bool(getattr(adapter, "_xcard_enabled", False))


def _refuse(message: str, *, retryable: bool = False, **extra) -> str:
    return tool_result({"ok": False, "error": message, "retryable": retryable, **extra})


async def _on_gateway(adapter, coro):
    """Run ``coro`` on the adapter's loop when the tool itself is on a worker thread."""
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    target = getattr(adapter, "_aio_loop", None)
    if target is None or target is current or not target.is_running():
        return await coro
    bridged = asyncio.run_coroutine_threadsafe(coro, target)
    return await asyncio.wrap_future(bridged)


def _clamp_wait(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _DEFAULT_WAIT
    if seconds != seconds:  # NaN
        seconds = _DEFAULT_WAIT
    return min(max(seconds, 5), _MAX_WAIT)


def _failure(exc) -> str:
    from gateway.platforms.yuanbao_xcard_service import XCardSendError

    if isinstance(exc, XCardSendError):
        return tool_result({
            "ok": False,
            "error": str(exc),
            "failure": exc.kind,
            "retryable": exc.kind == "too_large",
            "fallbackSent": exc.fallback_sent,
        })
    return tool_result({"ok": False, "error": str(exc), "retryable": False})


async def wait_extra(adapter, rec, group_code: str, args: dict) -> dict:
    """Extra result fields from blocking on a click (shared by send and template create/update)."""
    from gateway.platforms.yuanbao_xcard_registry import has_group_waiter, wait_for_surface_action
    from gateway.platforms.yuanbao_xcard_service import pause_reply_heartbeat

    if not args.get("waitForAction"):
        return {}
    if has_group_waiter(adapter, group_code):
        return {"waitDowngraded": True, "note": "this group already has a card waiting"}

    async def _wait():
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", f"group:{group_code}")
        async with pause_reply_heartbeat(adapter, chat_id):
            return await wait_for_surface_action(adapter, rec, _clamp_wait(args.get("waitTimeoutSec")))

    try:
        outcome = await _on_gateway(adapter, _wait())
    except Exception as exc:
        logger.exception("[xcard] wait failed")
        return {"waitStatus": "cancelled", "reason": str(exc)}
    if outcome.get("status") == "action":
        action = outcome["result"]
        return {
            "waitStatus": "action_received",
            "userAction": {
                "action": action.get("action"),
                "context": action.get("context"),
                "operatorUserId": action.get("operatorUserId"),
                "summary": action.get("summary"),
            },
            "note": "The click was returned here and will not also arrive as a new message.",
        }
    if outcome.get("status") == "timeout":
        return {"waitStatus": "timeout", "note": "The card is still active. A later click wakes a new turn."}
    reason = outcome.get("reason", "")
    if isinstance(reason, str) and reason.startswith("renderer_error:"):
        return {"waitStatus": "renderer_error", "reason": reason,
                "note": "The client could not render the card; it was closed and replaced with plain text. Ask for a text reply."}
    return {"waitStatus": "cancelled", "reason": reason}


async def yb_xcard_send(args: dict, **_kw) -> str:
    group_code = _group_code()
    if not group_code:
        return _refuse("xCard can only be sent in a Yuanbao group")
    try:
        adapter = _adapter()
    except _YbError as exc:
        return _refuse(str(exc))
    if not _enabled(adapter):
        return _refuse("xCard is disabled. Set platforms.yuanbao.extra.xcard_enabled: true and restart the gateway.")
    title = args.get("title") if isinstance(args.get("title"), str) else ""
    title = title.strip()
    if not title or len(title) > 80:
        return _refuse("title must be 1-80 characters", retryable=True)
    if args.get("mode") not in {"once", "perRevision"}:
        return _refuse("mode must be once or perRevision", retryable=True)
    fallback = args.get("fallbackText") if isinstance(args.get("fallbackText"), str) else ""
    fallback = fallback.strip() or title
    if len(fallback) > 2000:
        return _refuse("fallbackText must be at most 2000 characters", retryable=True)
    audience = "anyone" if args.get("audience") == "anyone" else "requester"
    owner = get_session_env("HERMES_SESSION_USER_ID", "")
    if audience == "requester" and not owner:
        return _refuse("the requesting user is unknown; pass audience=anyone")

    from gateway.platforms.yuanbao_xcard_service import XCardSendError, create_generated_card
    from gateway.platforms.yuanbao_xcard_validate import parse_generated_messages, validate_generated_messages

    parsed = parse_generated_messages(args.get("messages"))
    if "error" in parsed:
        return _refuse(parsed["error"], retryable=True)
    checked = validate_generated_messages(parsed["messages"])
    if not checked.ok:
        return tool_result({
            "ok": False,
            "error": "xCard A2UI validation failed",
            "errors": checked.errors[:30],
            "retryable": True,
        })

    async def _send():
        return await create_generated_card(
            adapter,
            group_code=group_code,
            session_key=get_session_env("HERMES_SESSION_KEY", ""),
            owner_user_id=owner,
            audience=audience,
            mode=args["mode"],
            title=title,
            fallback_text=fallback,
            messages=parsed["messages"],
        )

    try:
        rec = await _on_gateway(adapter, _send())
    except XCardSendError as exc:
        return _failure(exc)
    except Exception as exc:
        logger.exception("[xcard] send failed")
        return _failure(exc)

    result = {
        "ok": True,
        "surfaceId": rec.surface_id,
        "title": rec.title,
        "state": rec.state,
        "revision": rec.revision,
        "mode": rec.mode,
        "audience": rec.audience,
        "actions": rec.actions,
        "delivery": "card_already_sent",
        "assistantInstruction": "The card is already visible. Confirm briefly; do not repeat its contents.",
    }
    return tool_result({**result, **await wait_extra(adapter, rec, group_code, args)})


async def yb_xcard_update(args: dict, **_kw) -> str:
    group_code = _group_code()
    if not group_code:
        return _refuse("xCard can only be updated in a Yuanbao group")
    try:
        adapter = _adapter()
    except _YbError as exc:
        return _refuse(str(exc))
    if not _enabled(adapter):
        return _refuse("xCard is disabled. Set platforms.yuanbao.extra.xcard_enabled: true and restart the gateway.")
    surface_id = args.get("surfaceId") if isinstance(args.get("surfaceId"), str) else ""
    surface_id = surface_id.strip()
    if not surface_id:
        return _refuse("surfaceId is required", retryable=True)
    title = args.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title.strip()) > 80):
        return _refuse("title must be 1-80 characters", retryable=True)
    fallback = args.get("fallbackText")
    if fallback is not None and (not isinstance(fallback, str) or not fallback.strip() or len(fallback.strip()) > 2000):
        return _refuse("fallbackText must be 1-2000 characters", retryable=True)

    from gateway.platforms.yuanbao_xcard_registry import get_surface, lock_for
    from gateway.platforms.yuanbao_xcard_service import XCardSendError, update_generated_card
    from gateway.platforms.yuanbao_xcard_validate import parse_generated_messages, validate_generated_messages

    parsed = parse_generated_messages(args.get("messages"))
    if "error" in parsed:
        return _refuse(parsed["error"], retryable=True)
    checked = validate_generated_messages(parsed["messages"])
    if not checked.ok:
        return tool_result({
            "ok": False,
            "error": "xCard A2UI validation failed",
            "errors": checked.errors[:30],
            "retryable": True,
        })

    async def _update():
        async with lock_for(adapter, surface_id):
            rec = get_surface(adapter, surface_id)
            if rec is None:
                return _refuse("card not found; it may have been created before the gateway restarted")
            session_key = get_session_env("HERMES_SESSION_KEY", "")
            if rec.group_code != group_code or (rec.session_key and session_key and rec.session_key != session_key):
                return _refuse("that card does not belong to this session")
            if rec.state != "active":
                return _refuse(f"card is {rec.state} and can no longer be updated")
            next_title = title.strip() if isinstance(title, str) else rec.title
            next_fallback = fallback.strip() if isinstance(fallback, str) else rec.fallback_text
            await update_generated_card(adapter, rec, parsed["messages"], next_title, next_fallback)
            return tool_result({
                "ok": True,
                "surfaceId": rec.surface_id,
                "state": rec.state,
                "revision": rec.revision,
                "actions": rec.actions,
                "delivery": "existing_card_updated",
                "assistantInstruction": "The original card was updated. Do not send a replacement with yb_xcard_send.",
            })

    try:
        return await _on_gateway(adapter, _update())
    except XCardSendError as exc:
        return _failure(exc)
    except Exception as exc:
        logger.exception("[xcard] update failed")
        return _failure(exc)


_SEND_SCHEMA = {
    "name": "yb_xcard_send",
    "description": (
        "Send a self-contained Yuanbao xCard (A2UI v1.0) into the current group. "
        "Use it when a choice, confirmation, short form, or structured list is clearer than another round of text. "
        "Do not use it for a plain answer. Pass the full JSONL or JSON message array as messages. "
        "The card must be self-contained: no WebCard, xCardTemplate, tracking, @hostEnv, or unregistered host functions. "
        "Include a root component and put a human-readable resultText in every action context. "
        "mode=once closes the card on the first accepted click; mode=perRevision keeps it open for another click. "
        "Set waitForAction only when this turn cannot continue without the click. "
        "On success the card is already visible: do not repeat its contents. "
        "Later changes go through yb_xcard_update on the returned surfaceId."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "messages": {"type": "string", "description": "Full xCard JSONL, or a JSON array of A2UI messages."},
            "title": {"type": "string", "description": "Card title, 1-80 characters."},
            "fallbackText": {"type": "string", "description": "Plain text used when the client cannot render the card."},
            "audience": {
                "type": "string",
                "enum": ["requester", "anyone"],
                "description": "requester (default) or anyone in the group.",
            },
            "mode": {
                "type": "string",
                "enum": ["once", "perRevision"],
                "description": "once closes after the first accepted click; perRevision accepts one click per revision.",
            },
            "waitForAction": {"type": "boolean", "description": "Block until the user clicks or the wait times out."},
            "waitTimeoutSec": {"type": "number", "description": "Wait timeout in seconds, default 120, max 300."},
        },
        "required": ["messages", "title", "mode"],
    },
}

_UPDATE_SCHEMA = {
    "name": "yb_xcard_update",
    "description": (
        "Update an xCard previously sent with yb_xcard_send. Replaces the card in place; it does not send a new card. "
        "messages must be the full new A2UI snapshot, still including createSurface. "
        "Only an active card in this group session can be updated. "
        "The client must have echoed the card message id (the first click does this) before an update can be delivered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "surfaceId": {"type": "string", "description": "surfaceId returned by yb_xcard_send or the action callback."},
            "messages": {"type": "string", "description": "Full replacement xCard JSONL or JSON message array."},
            "title": {"type": "string", "description": "Optional new title, 1-80 characters."},
            "fallbackText": {"type": "string", "description": "Optional new plain-text fallback, up to 2000 characters."},
        },
        "required": ["surfaceId", "messages"],
    },
}

registry.register(
    name="yb_xcard_send",
    toolset="hermes-yuanbao",
    schema=_SEND_SCHEMA,
    handler=yb_xcard_send,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="🃏",
    execution_timeout=360,
)
registry.register(
    name="yb_xcard_update",
    toolset="hermes-yuanbao",
    schema=_UPDATE_SCHEMA,
    handler=yb_xcard_update,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="✏️",
)
