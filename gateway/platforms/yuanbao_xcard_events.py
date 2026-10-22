"""Accept or reject an xCard click, then either unblock a waiting tool or wake the group session.

The receipt only says whether the click was accepted. The model hears the result through the
waiting tool result or a new turn, never through a second receipt. A click that arrives while
the tool is blocked must not also be queued: the yuanbao group queue waits until the session
is idle, so queueing it would deadlock until the wait timed out.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.platforms.yuanbao_xcard_protocol import (
    EVENT_PROTOCOL,
    MIN_RENDERER_VERSION_ERROR,
    receipt_body,
)
from gateway.platforms.yuanbao_xcard_registry import (
    PLUGIN_EPOCH,
    Surface,
    get_surface,
    lock_for,
    record_action,
    remember_echo,
    remember_request,
    request_key,
    resolve_waiter,
)
from gateway.platforms.yuanbao_xcard_service import push_surface_update, send_group_body, send_group_text

logger = logging.getLogger(__name__)
_MAX_NAME = 64


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _action_body(rec: Surface, result: dict) -> str:
    import json

    data = {
        "kind": "yuanbao_xcard_action",
        "surfaceId": rec.surface_id,
        "source": rec.kind,
        **({"template": rec.template} if rec.template else {}),
        "title": rec.title,
        "action": result.get("action"),
        "summary": result.get("summary"),
        "context": result.get("context") or {},
        "operatorUserId": result.get("operatorUserId"),
        "revision": result.get("revision"),
        "cardState": rec.state,
    }
    if rec.state == "active" and rec.kind == "template":
        tail = (
            f"The card is still interactive. To change it, call yb_xcard_template with action=update "
            f"on surfaceId={rec.surface_id}. Do not create a replacement card."
        )
    elif rec.state == "active":
        tail = (
            f"The card is still interactive. To change it, call yb_xcard_update on surfaceId={rec.surface_id}. "
            "Do not call yb_xcard_send to create a replacement card."
        )
    else:
        tail = "The card is closed."
    summary = result.get("summary") or f"triggered {result.get('action')}"
    return (
        f"[xCard action] The user {summary} on the card \"{rec.title}\" you sent earlier.\n"
        f"This is a structured card event, not typed text. Continue the task from it. {tail}\n"
        f"{json.dumps(data, ensure_ascii=False)}"
    )


async def _send_receipt(adapter: Any, *, group_code: str, surface_id: str, request_id: str,
                        card_message_id: str, status: str, reason: str = "") -> None:
    body = receipt_body(
        card_message_id=card_message_id,
        surface_id=surface_id,
        request_id=request_id,
        status=status,
        reason=reason,
    )
    try:
        await send_group_body(adapter, group_code, body)
    except Exception:
        logger.exception("[xcard] receipt send failed surface=%s status=%s", surface_id, status)


def _payload_error(payload: dict) -> Optional[str]:
    event = payload.get("event") if _is_obj(payload.get("event")) else {}
    action = event.get("action") if _is_obj(event.get("action")) else None
    error = event.get("error") if _is_obj(event.get("error")) else None
    surface_id = payload.get("surfaceId") or (action or error or {}).get("surfaceId")
    if payload.get("protocol") != EVENT_PROTOCOL or not payload.get("requestId") or not surface_id:
        return "invalid_payload"
    if bool(action) == bool(error):
        return "invalid_payload"
    inner = (action or error or {}).get("surfaceId")
    if inner is not None and inner != surface_id:
        return "invalid_payload"
    return None


def _binding(payload: dict) -> dict:
    binding = payload.get("binding")
    return binding if _is_obj(binding) else {}


def _decide(rec: Optional[Surface], payload: dict, operator: str, group_code: str) -> dict:
    if rec is None:
        return {"kind": "reject", "status": "unknown", "reason": "unknown_surface"}
    request_id = str(payload.get("requestId") or "")
    prior = rec.seen.get(request_key(operator, request_id))
    if prior:
        return {"kind": "replay", "status": prior["status"], "reason": prior.get("reason", ""), "rec": rec}
    if group_code and group_code != rec.group_code:
        return {"kind": "reject", "status": "rejected", "reason": "permission_denied", "rec": rec}
    if rec.state == "expired":
        return {"kind": "reject", "status": "rejected", "reason": "surface_expired", "rec": rec}
    if rec.state != "active":
        return {"kind": "reject", "status": "rejected", "reason": "surface_closed", "rec": rec}
    if rec.audience == "requester" and operator != rec.owner_user_id:
        return {"kind": "reject", "status": "rejected", "reason": "permission_denied", "rec": rec}
    revision = _binding(payload).get("revision")
    if isinstance(revision, (int, float)) and not isinstance(revision, bool) and int(revision) != rec.revision:
        return {"kind": "reject", "status": "rejected", "reason": "stale_revision", "rec": rec}
    if rec.revision in rec.accepted_revisions:
        return {"kind": "reject", "status": "rejected", "reason": "stale_revision", "rec": rec}
    action = (payload.get("event") or {}).get("action") or {}
    name = action.get("name").strip() if isinstance(action.get("name"), str) else ""
    if not name or len(name) > _MAX_NAME or name not in rec.actions:
        return {"kind": "reject", "status": "rejected", "reason": "invalid_action", "rec": rec}
    context = action.get("context") if _is_obj(action.get("context")) else {}
    user_message = action.get("userMessage") if isinstance(action.get("userMessage"), str) else ""
    result = {
        "requestId": request_id,
        "operatorUserId": operator,
        "action": name,
        "context": context,
        "summary": user_message.strip() or f"触发了事件「{name}」",
        "userMessage": user_message.strip(),
        "sourceComponentId": action.get("sourceComponentId") if isinstance(action.get("sourceComponentId"), str) else "",
        "revision": rec.revision,
    }
    if rec.kind != "template":
        return {"kind": "accept", "rec": rec, "result": result}
    from gateway.platforms.yuanbao_xcard_templates import get_template

    tpl = get_template(rec.template)
    if tpl is None or name not in tpl.events:
        return {"kind": "reject", "status": "rejected", "reason": "invalid_action", "rec": rec}
    verdict = tpl.decide_action(rec.data, name, context)
    if "reject" in verdict:
        return {"kind": "reject", "status": "rejected", "reason": verdict["reject"], "rec": rec}
    result["context"] = verdict.get("context") or {}
    result["summary"] = verdict.get("summary") or result["summary"]
    return {"kind": "accept", "rec": rec, "result": result, "keepOpen": bool(verdict.get("keepOpen"))}


async def _wake(adapter: Any, rec: Surface, result: dict) -> None:
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.wake import admit_internal_event

    source = SessionSource(
        platform=getattr(adapter, "platform", None) or Platform.YUANBAO,
        chat_id=f"group:{rec.group_code}",
        chat_type="group",
        user_id=result.get("operatorUserId") or None,
        thread_id="main",
    )
    if hasattr(adapter, "build_source"):
        try:
            source = adapter.build_source(
                chat_id=f"group:{rec.group_code}",
                chat_type="group",
                user_id=result.get("operatorUserId") or None,
                thread_id="main",
            )
        except Exception:
            logger.debug("[xcard] build_source failed; using a direct group source", exc_info=True)
    event = MessageEvent(
        text=_action_body(rec, result),
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        message_id=f"xcard:{rec.surface_id}:{result.get('requestId')}",
    )
    # Receipt means the adapter queued/started the turn; a bare handle_message return is not enough.
    await admit_internal_event(adapter, event)


async def _apply_renderer_error(adapter: Any, rec: Surface, payload: dict, operator: str, group_code: str) -> None:
    error = (payload.get("event") or {}).get("error") or {}
    key = request_key(operator, str(payload.get("requestId") or ""))
    prior = rec.seen.get(key)
    if prior:
        await _send_receipt(
            adapter, group_code=group_code, surface_id=rec.surface_id,
            request_id=str(payload.get("requestId") or ""), card_message_id=rec.card_message_id,
            status=prior["status"], reason=prior.get("reason", ""),
        )
        return
    code = error.get("code") if isinstance(error.get("code"), str) and error.get("code") else "UNKNOWN"
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    rec.last_error = {"code": code, "message": message[:500]}
    remember_request(rec, key, "accepted")
    await _send_receipt(
        adapter, group_code=group_code, surface_id=rec.surface_id,
        request_id=str(payload.get("requestId") or ""), card_message_id=rec.card_message_id, status="accepted",
    )
    if code == MIN_RENDERER_VERSION_ERROR and rec.state == "active":
        rec.state = "cancelled"
        if rec.waiter is not None and not rec.waiter.done():
            rec.waiter.get_loop().call_soon_threadsafe(
                rec.waiter.set_result, {"status": "cancelled", "reason": f"renderer_error:{code}"},
            )
            rec.waiter = None
        try:
            await push_surface_update(adapter, rec)
        except Exception:
            logger.exception("[xcard] close after renderer error failed surface=%s", rec.surface_id)
        await send_group_text(adapter, rec.group_code, rec.fallback_text)
    remember_request(rec, key, "completed")


async def handle_up_event(adapter: Any, *, group_code: str, operator_user_id: str, payload: dict) -> None:
    if not operator_user_id or not group_code or not isinstance(payload, dict):
        logger.warning("[xcard] upevent missing sender or group")
        return
    surface_id = str(payload.get("surfaceId") or "")
    request_id = str(payload.get("requestId") or "")
    card_message_id = _binding(payload).get("messageId") if isinstance(_binding(payload).get("messageId"), str) else ""
    problem = _payload_error(payload)
    if problem:
        await _send_receipt(
            adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
            card_message_id=card_message_id, status="rejected", reason=problem,
        )
        return
    epoch = _binding(payload).get("pluginEpoch")
    if isinstance(epoch, str) and epoch and epoch != PLUGIN_EPOCH:
        await _send_receipt(
            adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
            card_message_id=card_message_id, status="rejected", reason="epoch_expired",
        )
        return
    known = get_surface(adapter, surface_id)
    if known is None:
        await _send_receipt(
            adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
            card_message_id=card_message_id, status="unknown", reason="unknown_surface",
        )
        return
    if known.card_message_id and card_message_id and known.card_message_id != card_message_id:
        await _send_receipt(
            adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
            card_message_id=card_message_id, status="rejected", reason="invalid_payload",
        )
        return
    event = payload.get("event") if _is_obj(payload.get("event")) else {}
    if _is_obj(event.get("error")):
        if known.audience == "requester" and operator_user_id != known.owner_user_id:
            await _send_receipt(
                adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
                card_message_id=known.card_message_id, status="rejected", reason="permission_denied",
            )
            return
        async with lock_for(adapter, surface_id):
            if card_message_id and not known.card_message_id:
                known.card_message_id = card_message_id
            await _apply_renderer_error(adapter, known, payload, operator_user_id, group_code)
        return

    delivered = False
    result: Optional[dict] = None
    async with lock_for(adapter, surface_id):
        decision = _decide(get_surface(adapter, surface_id), payload, operator_user_id, group_code)
        if decision["kind"] == "replay":
            await _send_receipt(
                adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
                card_message_id=decision["rec"].card_message_id or card_message_id,
                status=decision["status"], reason=decision.get("reason") or "",
            )
            return
        if decision["kind"] == "reject":
            rec = decision.get("rec")
            if rec is not None:
                remember_request(rec, request_key(operator_user_id, request_id), decision["status"], decision["reason"])
            await _send_receipt(
                adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
                card_message_id=(rec.card_message_id if rec else "") or card_message_id,
                status=decision["status"], reason=decision["reason"],
            )
            return
        rec = decision["rec"]
        result = decision["result"]
        if card_message_id and not rec.card_message_id:
            rec.card_message_id = card_message_id
        rec.accepted_revisions.add(rec.revision)
        record_action(rec, result)
        if result.get("userMessage"):
            remember_echo(adapter, rec.group_code, operator_user_id, result["userMessage"])
        remember_request(rec, request_key(operator_user_id, request_id), "accepted")
        await _send_receipt(
            adapter, group_code=group_code, surface_id=surface_id, request_id=request_id,
            card_message_id=rec.card_message_id, status="accepted",
        )
        if rec.mode == "once" and not decision.get("keepOpen"):
            rec.state = "completed"
        try:
            await push_surface_update(adapter, rec)
        except Exception:
            logger.exception("[xcard] post-accept update failed surface=%s", surface_id)
        delivered = resolve_waiter(rec, result)
    if result is None:
        return
    key = request_key(operator_user_id, request_id)
    if delivered:
        remember_request(known, key, "completed")
        return
    try:
        await _wake(adapter, known, result)
        remember_request(known, key, "completed")
    except Exception:
        logger.exception("[xcard] wake failed surface=%s", surface_id)
        remember_request(known, key, "failed", "dispatch_failed")
