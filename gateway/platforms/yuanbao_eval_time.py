"""Yuanbao eval helpers — channel_prompt wall clock + HERMES_YB_TIME_OFFSET mock.

Used only when ``HERMES_YB_TIME_OFFSET`` is set.  Does not affect
SignManager sign-token timestamps or global ``hermes_time.now()``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from gateway.platforms.yuanbao import InboundContext

logger = logging.getLogger(__name__)

_BEIJING_TZ = timezone(timedelta(hours=8))

_YB_TIME_OFFSET_RE = re.compile(
    r"^(?P<sign>[+-])?(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd]?)$",
    re.IGNORECASE,
)

_YB_OFFSET_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _parse_beijing_time_offset() -> timedelta:
    """Parse ``HERMES_YB_TIME_OFFSET`` (e.g. ``+1d``, ``-2h``, ``3600``)."""
    raw = os.getenv("HERMES_YB_TIME_OFFSET", "").strip()
    if not raw:
        return timedelta(0)

    match = _YB_TIME_OFFSET_RE.match(raw)
    if not match:
        logger.warning(
            "Invalid HERMES_YB_TIME_OFFSET %r; using zero offset.", raw,
        )
        return timedelta(0)

    sign = -1 if match.group("sign") == "-" else 1
    value = float(match.group("value"))
    unit = (match.group("unit") or "s").lower()
    seconds = _YB_OFFSET_UNITS.get(unit)
    if seconds is None:
        logger.warning(
            "Invalid HERMES_YB_TIME_OFFSET unit in %r; using zero offset.", raw,
        )
        return timedelta(0)

    return timedelta(seconds=sign * value * seconds)


def beijing_wall_clock_for_display() -> datetime:
    """Beijing wall clock plus optional ``HERMES_YB_TIME_OFFSET``."""
    return datetime.now(_BEIJING_TZ) + _parse_beijing_time_offset()


def format_beijing_wall_clock() -> str:
    """Format display time for Yuanbao channel_prompt injection."""
    return beijing_wall_clock_for_display().strftime(
        "%Y-%m-%d %H:%M:%S (Beijing, UTC+8)",
    )


def channel_prompt_time_enabled() -> bool:
    """True when HERMES_YB_TIME_OFFSET is set (inject wall clock into channel_prompt)."""
    return bool(os.getenv("HERMES_YB_TIME_OFFSET", "").strip())


def build_channel_prompt_time_line() -> str:
    """Build the ``**Current time:** ...`` line for channel_prompt."""
    return f"**Current time:** {format_beijing_wall_clock()}"


def make_channel_prompt_time_middleware(
    inbound_middleware_base: type,
) -> type:
    """Return a middleware class (subclass of *inbound_middleware_base*)."""

    class ChannelPromptTimeMiddleware(inbound_middleware_base):
        """Eval-only: prepend Beijing wall clock to channel_prompt (ephemeral SP)."""

        name = "channel-prompt-time"

        async def handle(
            self, ctx: InboundContext, next_fn: Callable,
        ) -> None:
            if not channel_prompt_time_enabled():
                await next_fn()
                return

            existing = (ctx.channel_prompt or "").strip()

            def _refresh() -> str:
                line = build_channel_prompt_time_line()
                return f"{line}\n\n{existing}" if existing else line

            ctx.channel_prompt = _refresh()
            ctx.channel_prompt_refresh = _refresh
            await next_fn()

    return ChannelPromptTimeMiddleware
