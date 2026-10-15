"""Create and update an xCard, with one plain-text fallback when the server rejects the card."""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from gateway.platforms.yuanbao_xcard_protocol import (
    MAX_CARD_CONTENT_BYTES,
    card_messages_body,
    down_event_body,
    text_body,
    wire_bytes,
)
from gateway.platforms.yuanbao_xcard_registry import (
    PLUGIN_EPOCH,
    Surface,
    fallback_is_recent,
    mark_fallback,
    register_surface,
    touch_ttl,
)
from gateway.platforms.yuanbao_xcard_validate import extract_actions, render_generated_messages

logger = logging.getLogger(__name__)
_RATE_LIMITED = re.compile(r"rate\s*limit", re.IGNORECASE)


class XCardSendError(Exception):
    def __init__(self, message: str, kind: str = "error", fallback_sent: bool = False):
        super().__init__(message)
        self.kind = kind
        self.fallback_sent = fallback_sent


def classify_send(response: Any) -> tuple[bool, str, str]:
    """``(ok, kind, message)``. A ``success`` reply whose text says rate limited was dropped."""
    if not isinstance(response, dict):
        return False, "error", "empty send response"
    message = str(response.get("message") or response.get("error") or "")
    code = response.get("code")
    failed = response.get("success") is False or (isinstance(code, int) and not isinstance(code, bool) and code != 0)
    if failed:
        return False, "rejected", message or (f"code {code}" if code is not None else "send failed")
    if _RATE_LIMITED.search(message):
        return False, "rate_limited", message
    return True, "", ""


async def send_group_body(adapter: Any, group_code: str, body: list) -> dict:
    hook = getattr(adapter, "_xcard_send", None)
    if hook is not None:
        return await hook(group_code, body)
    return await adapter._outbound.sender.send_group_msg_body(group_code, body)


def _extension(rec: Surface) -> dict:
    return {
        "schemaVersion": 1,
        "pluginEpoch": PLUGIN_EPOCH,
        "revision": rec.revision,
        "ownerUserId": rec.owner_user_id,
        "audience": rec.audience,
        "mode": rec.mode,
        "state": rec.state,
        "fallbackText": rec.fallback_text,
        "eventTransport": "hermes",
        "source": "generated",
        "actionSchemas": {name: {"type": "object"} for name in rec.actions},
    }


def render_messages(rec: Surface, *, updates_only: bool = False) -> list:
    messages = render_generated_messages(
        rec.messages,
        rec.surface_id,
        _extension(rec),
        state=rec.state,
        revision=rec.revision,
        last_result=rec.last_result,
    )
    if updates_only:
        return [message for message in messages if "createSurface" not in message]
    return messages


def _check_size(body: list) -> list:
    size = wire_bytes(body)
    if size > MAX_CARD_CONTENT_BYTES:
        raise XCardSendError(
            f"card content is {size} bytes, over the {MAX_CARD_CONTENT_BYTES} byte limit",
            "too_large",
        )
    return body


async def send_group_text(adapter: Any, group_code: str, text: str) -> bool:
    if fallback_is_recent(adapter, group_code, text):
        return True
    try:
        response = await send_group_body(adapter, group_code, text_body(text))
    except Exception:
        logger.exception("[xcard] text fallback failed group=%s", group_code)
        return False
    ok, kind, message = classify_send(response)
    if not ok:
        logger.warning("[xcard] text fallback rejected kind=%s message=%s", kind, message)
        return False
    mark_fallback(adapter, group_code, text)
    return True


async def create_generated_card(
    adapter: Any,
    *,
    group_code: str,
    session_key: str,
    owner_user_id: str,
    audience: str,
    mode: str,
    title: str,
    fallback_text: str,
    messages: list,
) -> Surface:
    rec = Surface(
        surface_id=str(uuid.uuid4()),
        group_code=group_code,
        session_key=session_key,
        owner_user_id=owner_user_id,
        audience=audience,
        mode=mode,
        title=title,
        fallback_text=fallback_text,
        messages=messages,
        actions=extract_actions(messages),
    )
    body = _check_size(card_messages_body(render_messages(rec)))
    try:
        response = await send_group_body(adapter, group_code, body)
    except Exception as exc:
        raise XCardSendError(f"card send failed: {exc}") from exc
    ok, kind, message = classify_send(response)
    if not ok:
        fallback_sent = False
        if kind == "rejected":
            fallback_sent = await send_group_text(adapter, group_code, fallback_text)
        raise XCardSendError(f"card send failed: {message}", kind, fallback_sent)
    register_surface(adapter, rec)
    logger.info("[xcard] created surface=%s group=%s", rec.surface_id, group_code)
    return rec


async def push_surface_update(adapter: Any, rec: Surface) -> None:
    """Re-render at revision+1. The caller holds no lock across this await; roll the revision back on failure."""
    if not rec.card_message_id:
        raise XCardSendError("card has no server message id yet, so it cannot be updated")
    rec.revision += 1
    try:
        body = _check_size(down_event_body(
            card_message_id=rec.card_message_id,
            surface_id=rec.surface_id,
            revision=rec.revision,
            messages=render_messages(rec, updates_only=True),
        ))
        response = await send_group_body(adapter, rec.group_code, body)
        ok, kind, message = classify_send(response)
        if not ok:
            raise XCardSendError(f"card update failed: {message}", kind)
        touch_ttl(rec)
    except Exception:
        rec.revision -= 1
        raise


async def update_generated_card(
    adapter: Any,
    rec: Surface,
    messages: list,
    title: str,
    fallback_text: str,
) -> None:
    previous = (rec.messages, rec.actions, rec.title, rec.fallback_text)
    rec.messages = messages
    rec.actions = extract_actions(messages)
    rec.title = title
    rec.fallback_text = fallback_text
    try:
        await push_surface_update(adapter, rec)
    except Exception:
        rec.messages, rec.actions, rec.title, rec.fallback_text = previous
        raise


@asynccontextmanager
async def pause_reply_heartbeat(adapter: Any, chat_id: str):
    """Stop the 2s RUNNING bubble while a tool is blocked on a click; it would trip the rate limit."""
    heartbeat = getattr(getattr(adapter, "_outbound", None), "heartbeat", None)
    task = None if heartbeat is None else heartbeat._reply_heartbeat_tasks.get(chat_id)
    paused = task is not None and not task.done()
    if paused:
        await heartbeat.stop(chat_id, send_finish=False)
    try:
        yield
    finally:
        if paused:
            await heartbeat.start(chat_id)
