"""Inbound intercept for xCard upevents and button-text echoes.

Runs after access control and before content extraction, so a click is not turned into
``[unsupported message type]`` and does not start a chat turn. With ``xcard_enabled`` off,
every message continues down the existing pipeline.
"""

from __future__ import annotations

import asyncio
import logging

from gateway.platforms.yuanbao import InboundMiddleware
from gateway.platforms.yuanbao_xcard_events import handle_up_event
from gateway.platforms.yuanbao_xcard_protocol import parse_up_event, plain_text
from gateway.platforms.yuanbao_xcard_registry import consume_user_message_echo

logger = logging.getLogger(__name__)


def _note_loop(adapter) -> None:
    if getattr(adapter, "_aio_loop", None) is None:
        try:
            adapter._aio_loop = asyncio.get_running_loop()
        except RuntimeError:
            return


class XCardUpEventMiddleware(InboundMiddleware):
    name = "xcard-upevent"

    async def handle(self, ctx, next_fn) -> None:
        adapter = ctx.adapter
        if not getattr(adapter, "_xcard_enabled", False) or ctx.chat_type != "group" or not ctx.group_code:
            await next_fn()
            return
        _note_loop(adapter)
        payload = parse_up_event(ctx.msg_body)
        if payload is not None:
            try:
                await handle_up_event(
                    adapter,
                    group_code=ctx.group_code,
                    operator_user_id=ctx.from_account or "",
                    payload=payload,
                )
            except Exception:
                logger.exception("[xcard] upevent handler failed group=%s", ctx.group_code)
            return
        text = plain_text(ctx.msg_body)
        if text and consume_user_message_echo(adapter, ctx.group_code, ctx.from_account or "", text):
            logger.info("[xcard] dropped userMessage echo group=%s from=%s", ctx.group_code, ctx.from_account)
            return
        await next_fn()
