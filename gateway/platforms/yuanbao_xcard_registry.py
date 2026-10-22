"""Per-adapter xCard surfaces.

State lives on the Yuanbao adapter that owns the bot, not in a process-global map: one gateway
process serves many profiles, and each profile has its own adapter. Nothing is persisted.
``PLUGIN_EPOCH`` changes on process start and is embedded in the card, so a click on a card
from a previous process is answered ``epoch_expired`` instead of being bound to a new session.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

SURFACE_TTL_S = 30 * 60
MAX_SURFACES = 200
MAX_SEEN = 50
MAX_HISTORY = 10
ECHO_TTL_S = 30
PLUGIN_EPOCH = uuid.uuid4().hex[:16]


@dataclass
class Surface:
    surface_id: str
    group_code: str
    session_key: str
    owner_user_id: str
    audience: str
    mode: str
    title: str
    fallback_text: str
    messages: list
    actions: list[str]
    kind: str = "generated"  # generated | template
    template: str = ""
    data: dict = field(default_factory=dict)
    revision: int = 1
    state: str = "active"  # active | completed | cancelled | expired
    card_message_id: str = ""
    created_at: float = 0
    expires_at: float = 0
    accepted_revisions: set[int] = field(default_factory=set)
    seen: dict[str, dict] = field(default_factory=dict)
    last_result: Optional[dict] = None
    history: list[dict] = field(default_factory=list)
    last_error: Optional[dict] = None
    waiter: Optional[asyncio.Future] = None


@dataclass
class XCardState:
    surfaces: dict[str, Surface] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    echoes: dict[str, float] = field(default_factory=dict)
    fallbacks: dict[str, float] = field(default_factory=dict)


def state_of(adapter: Any) -> XCardState:
    found = getattr(adapter, "_xcard_state", None)
    if found is None:
        found = XCardState()
        adapter._xcard_state = found
    return found


def account_id(adapter: Any) -> str:
    return str(getattr(adapter, "_bot_id", None) or "default")


def request_key(operator_user_id: str, request_id: str) -> str:
    return f"{operator_user_id}\n{request_id}"


def _sweep(bucket: XCardState, now: float) -> None:
    for surface_id, rec in list(bucket.surfaces.items()):
        _mark_expired(rec, now)
        if rec.state != "active" and now > rec.expires_at + SURFACE_TTL_S:
            _cancel_waiter(rec, "expired")
            bucket.surfaces.pop(surface_id, None)
            bucket.locks.pop(surface_id, None)


def _mark_expired(rec: Surface, now: float) -> None:
    if rec.state == "active" and now > rec.expires_at:
        rec.state = "expired"
        _cancel_waiter(rec, "expired")


def _evict(bucket: XCardState, now: float) -> None:
    overflow = len(bucket.surfaces) - MAX_SURFACES
    if overflow <= 0:
        return
    ordered = sorted(bucket.surfaces.values(), key=lambda rec: rec.created_at)
    for rec in ordered[:overflow]:
        _cancel_waiter(rec, "evicted")
        bucket.surfaces.pop(rec.surface_id, None)
        bucket.locks.pop(rec.surface_id, None)


def register_surface(adapter: Any, rec: Surface) -> Surface:
    bucket = state_of(adapter)
    now = time.time()
    rec.created_at = now
    rec.expires_at = now + SURFACE_TTL_S
    _sweep(bucket, now)
    bucket.surfaces[rec.surface_id] = rec
    _evict(bucket, now)
    return rec


def get_surface(adapter: Any, surface_id: str) -> Optional[Surface]:
    rec = state_of(adapter).surfaces.get(surface_id)
    if rec is not None:
        _mark_expired(rec, time.time())
    return rec


def list_surfaces(adapter: Any, group_code: str = "") -> list[Surface]:
    bucket = state_of(adapter)
    _sweep(bucket, time.time())
    return [rec for rec in bucket.surfaces.values() if not group_code or rec.group_code == group_code]


def lock_for(adapter: Any, surface_id: str) -> asyncio.Lock:
    """Create the lock on the loop that first awaits it (the gateway loop)."""
    bucket = state_of(adapter)
    lock = bucket.locks.get(surface_id)
    if lock is None:
        lock = asyncio.Lock()
        bucket.locks[surface_id] = lock
    return lock


def remember_request(rec: Surface, key: str, status: str, reason: str = "") -> None:
    rec.seen[key] = {"status": status, **({"reason": reason} if reason else {})}
    while len(rec.seen) > MAX_SEEN:
        rec.seen.pop(next(iter(rec.seen)))


def record_action(rec: Surface, result: dict) -> None:
    rec.last_result = result
    rec.history = [*rec.history, result][-MAX_HISTORY:]


def has_group_waiter(adapter: Any, group_code: str) -> bool:
    return any(
        rec.waiter is not None and rec.group_code == group_code
        for rec in state_of(adapter).surfaces.values()
    )


def _cancel_waiter(rec: Surface, reason: str) -> None:
    waiter = rec.waiter
    if waiter is None or waiter.done():
        rec.waiter = None
        return
    rec.waiter = None
    waiter.get_loop().call_soon_threadsafe(waiter.set_result, {"status": "cancelled", "reason": reason})


def resolve_waiter(rec: Surface, result: dict) -> bool:
    waiter = rec.waiter
    if waiter is None or waiter.done():
        return False
    rec.waiter = None
    waiter.get_loop().call_soon_threadsafe(waiter.set_result, {"status": "action", "result": result})
    return True


async def wait_for_surface_action(adapter: Any, rec: Surface, timeout_s: float) -> dict:
    """Block until an accepted click, timeout, or cancellation. The future is armed before the first await."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    rec.waiter = future
    try:
        return await asyncio.wait_for(future, timeout_s)
    except TimeoutError:
        return {"status": "timeout"}
    finally:
        if rec.waiter is future:
            rec.waiter = None


def remember_echo(adapter: Any, group_code: str, operator: str, text: str) -> None:
    cleaned = text.strip()
    if not cleaned:
        return
    bucket = state_of(adapter)
    now = time.time()
    for key, exp in list(bucket.echoes.items()):
        if exp < now:
            bucket.echoes.pop(key, None)
    bucket.echoes[_echo_key(group_code, operator, cleaned)] = now + ECHO_TTL_S


def consume_user_message_echo(adapter: Any, group_code: str, operator: str, text: str) -> bool:
    bucket = state_of(adapter)
    key = _echo_key(group_code, operator, text.strip())
    exp = bucket.echoes.pop(key, None)
    return exp is not None and exp >= time.time()


def _echo_key(group_code: str, operator: str, text: str) -> str:
    return "\n".join((group_code, operator, text.strip()))


def fallback_is_recent(adapter: Any, group_code: str, text: str) -> bool:
    """True when this exact fallback text was delivered to the group in the last five minutes."""
    bucket = state_of(adapter)
    now = time.time()
    key = f"{group_code}\n{text}"
    for item, at in list(bucket.fallbacks.items()):
        if now - at > 5 * 60:
            bucket.fallbacks.pop(item, None)
    return key in bucket.fallbacks


def mark_fallback(adapter: Any, group_code: str, text: str) -> None:
    state_of(adapter).fallbacks[f"{group_code}\n{text}"] = time.time()


def touch_ttl(rec: Surface) -> None:
    if rec.state == "active":
        rec.expires_at = time.time() + SURFACE_TTL_S
