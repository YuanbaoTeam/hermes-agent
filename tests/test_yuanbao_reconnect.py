"""test_yuanbao_reconnect.py - Yuanbao adapter reconnect/self-heal contracts.

Covers the SRE-hardening state machine in ``ConnectionManager``: backoff math,
the idempotent reconnect entry, multi-signal liveness, the tiered
fast -> slow -> give-up flow (incl. PermanentAuthError short-circuit), the
self_restart anti-storm rate limit, and background-task teardown in close().

These assert *behaviour contracts* (invariants / control flow), not frozen
values, and drive the state machine with lightweight fakes so no real network,
timers, or process exit are involved.
"""

import sys
import os
import time
import asyncio
import logging

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest
from gateway.config import PlatformConfig
from gateway.platforms import yuanbao
from gateway.platforms.yuanbao import (
    YuanbaoAdapter,
    ConnectionManager,
    PermanentAuthError,
    HEARTBEAT_TIMEOUT_THRESHOLD,
    SELF_RESTART_MAX_PER_HOUR,
    RECONNECT_FAST_CAP_S,
    RECONNECT_JITTER_RATIO,
    EV_WS_RECONNECT_ATTEMPT,
)
from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE


def make_config(reconnect=None, **kwargs):
    extra = kwargs.pop("extra", {})
    extra.setdefault("app_id", "test_key")
    extra.setdefault("app_secret", "test_secret")
    extra.setdefault("ws_url", "wss://test.example.com/ws")
    extra.setdefault("api_domain", "https://test.example.com")
    if reconnect:
        extra["reconnect"] = reconnect
    return PlatformConfig(extra=extra, **kwargs)


def _connection(reconnect=None) -> ConnectionManager:
    return YuanbaoAdapter(make_config(reconnect=reconnect))._connection


class _FakeTask:
    """Stand-in for asyncio.Task exposing only the .done() the code checks."""

    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


class _OpenWS:
    """Fake WS reporting an OPEN state for the liveness check."""

    def __init__(self):
        from websockets.protocol import State as WsState
        self.state = WsState.OPEN
        self.closed = False


async def _noop_sleep(*_a, **_k):
    return None


# --------------------------------------------------------------------------
# _compute_backoff — exponential backoff with jitter (seconds)
# --------------------------------------------------------------------------

def test_compute_backoff_is_exponential_and_capped(monkeypatch):
    """Without jitter: doubles per attempt, then clamps at the cap."""
    monkeypatch.setattr(yuanbao.random, "uniform", lambda _a, _b: 0.0)
    cm = _connection()

    assert cm._compute_backoff(0) == pytest.approx(1.0)
    assert cm._compute_backoff(1) == pytest.approx(2.0)
    assert cm._compute_backoff(2) == pytest.approx(4.0)
    # A large attempt saturates at the cap, never runs away.
    assert cm._compute_backoff(100) == pytest.approx(RECONNECT_FAST_CAP_S)


def test_compute_backoff_respects_floor_and_ceiling():
    """With real jitter, the result stays within [0.5, cap*(1+ratio)]."""
    cm = _connection()
    ceiling = RECONNECT_FAST_CAP_S * (1.0 + RECONNECT_JITTER_RATIO)
    for attempt in range(0, 20):
        wait = cm._compute_backoff(attempt)
        assert 0.5 <= wait <= ceiling


def test_compute_backoff_never_below_half_second(monkeypatch):
    """Negative jitter can't push the wait below the 0.5s floor."""
    monkeypatch.setattr(yuanbao.random, "uniform", lambda _a, _b: -1e6)
    cm = _connection()
    assert cm._compute_backoff(0) == pytest.approx(0.5)


def test_log_event_includes_envelope_in_text(caplog):
    """The default formatter only renders message text, so keep envelope grep-able."""
    cm = _connection()
    cm._client_conn_id = "abc123"
    cm._connected_at = time.monotonic() - 5
    cm._reconnect_attempts = 2
    cm._reconnect_phase = "fast"

    with caplog.at_level(logging.INFO):
        cm._log_event(logging.INFO, EV_WS_RECONNECT_ATTEMPT, phase="fast")

    message = caplog.records[-1].getMessage()
    assert "event=ws.reconnect.attempt" in message
    assert "conn_id=abc123" in message
    assert "reconnect_attempt=2" in message
    assert "reconnect_phase=fast" in message


# --------------------------------------------------------------------------
# _trigger_reconnect — single idempotent entry (C-5)
# --------------------------------------------------------------------------

def test_trigger_reconnect_is_idempotent_and_guarded():
    """Only one reconnect chain is ever scheduled; guards suppress the rest."""
    cm = _connection()
    cm._adapter._running = True

    scheduled = []

    def _record(reason):
        scheduled.append(reason)

    cm._schedule_reconnect_task = _record  # type: ignore[assignment]

    # First trigger schedules exactly one chain.
    cm._trigger_reconnect("first")
    assert scheduled == ["first"]

    # Already reconnecting -> skipped (no second redial).
    cm._reconnecting = True
    cm._trigger_reconnect("dup")
    assert scheduled == ["first"]
    cm._reconnecting = False

    # Stopping -> skipped.
    cm._stopping = True
    cm._trigger_reconnect("while_stopping")
    assert scheduled == ["first"]
    cm._stopping = False

    # Adapter not running -> skipped.
    cm._adapter._running = False
    cm._trigger_reconnect("not_running")
    assert scheduled == ["first"]


# --------------------------------------------------------------------------
# _is_ws_truly_alive / _dead_ws_reason — multi-signal liveness (C-4)
# --------------------------------------------------------------------------

def _make_healthy(cm: ConnectionManager) -> None:
    cm._ws = _OpenWS()
    cm._recv_task = _FakeTask(done=False)
    cm._heartbeat_task = _FakeTask(done=False)
    cm._consecutive_hb_timeouts = 0
    cm._last_msg_at = time.monotonic()


def test_ws_truly_alive_all_signals_pass():
    cm = _connection()
    _make_healthy(cm)
    assert cm._is_ws_truly_alive() is True


@pytest.mark.parametrize("break_signal, expected_reason", [
    ("ws_none", "ws_none"),
    ("recv_dead", "recv_task_dead"),
    ("hb_dead", "hb_task_dead"),
    ("hb_timeouts", "hb_timeouts"),
    ("stale", "stale_msg"),
])
def test_ws_truly_alive_fails_when_any_signal_breaks(break_signal, expected_reason):
    cm = _connection()
    _make_healthy(cm)

    if break_signal == "ws_none":
        cm._ws = None
    elif break_signal == "recv_dead":
        cm._recv_task = _FakeTask(done=True)
    elif break_signal == "hb_dead":
        cm._heartbeat_task = _FakeTask(done=True)
    elif break_signal == "hb_timeouts":
        cm._consecutive_hb_timeouts = HEARTBEAT_TIMEOUT_THRESHOLD
    elif break_signal == "stale":
        cm._last_msg_at = time.monotonic() - 10_000

    assert cm._is_ws_truly_alive() is False
    assert cm._dead_ws_reason() == expected_reason


# --------------------------------------------------------------------------
# _do_reconnect — tiered state machine (fast -> slow -> give-up, P-2)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_do_reconnect_returns_on_first_success(monkeypatch):
    """A successful redial returns True without entering the slow stage."""
    monkeypatch.setattr(yuanbao.asyncio, "sleep", _noop_sleep)
    cm = _connection(reconnect={"fast_attempts": 5, "slow_max_hours": 1.0})

    calls = {"attempt": 0, "give_up": 0}

    async def _attempt():
        calls["attempt"] += 1
        return True

    async def _give_up(**_kw):
        calls["give_up"] += 1
        return False

    cm._attempt_single_reconnect = _attempt  # type: ignore[assignment]
    cm._handle_give_up = _give_up  # type: ignore[assignment]

    assert await cm._do_reconnect() is True
    assert calls["attempt"] == 1
    assert calls["give_up"] == 0


@pytest.mark.asyncio
async def test_do_reconnect_permanent_auth_short_circuits(monkeypatch):
    """PermanentAuthError bails to give-up immediately, not after N attempts."""
    monkeypatch.setattr(yuanbao.asyncio, "sleep", _noop_sleep)
    cm = _connection(reconnect={"fast_attempts": 5, "slow_max_hours": 1.0})

    calls = {"attempt": 0}
    give_up = {}

    async def _attempt():
        calls["attempt"] += 1
        raise PermanentAuthError("bad credentials")

    async def _give_up(*, reason, detail=""):
        give_up["reason"] = reason
        return False

    cm._attempt_single_reconnect = _attempt  # type: ignore[assignment]
    cm._handle_give_up = _give_up  # type: ignore[assignment]

    assert await cm._do_reconnect() is False
    # Short-circuited on the very first attempt — retry budget untouched.
    assert calls["attempt"] == 1
    assert give_up["reason"] == "permanent_auth"


@pytest.mark.asyncio
async def test_do_reconnect_exhausts_then_gives_up(monkeypatch):
    """Transient failures burn the fast budget, then give up as 'exhausted'."""
    monkeypatch.setattr(yuanbao.asyncio, "sleep", _noop_sleep)
    # slow_max_hours=0 makes the slow deadline already-passed, so we go
    # straight from the fast budget to give-up.
    cm = _connection(reconnect={"fast_attempts": 3, "slow_max_hours": 0.0})

    calls = {"attempt": 0}
    give_up = {}

    async def _attempt():
        calls["attempt"] += 1
        return False

    async def _give_up(*, reason, detail=""):
        give_up["reason"] = reason
        return False

    cm._attempt_single_reconnect = _attempt  # type: ignore[assignment]
    cm._handle_give_up = _give_up  # type: ignore[assignment]

    assert await cm._do_reconnect() is False
    assert calls["attempt"] == 3
    assert give_up["reason"] == "exhausted"


# --------------------------------------------------------------------------
# _schedule_self_restart — anti-storm rate limit (C-9)
# --------------------------------------------------------------------------

def test_self_restart_under_limit_schedules_retry(monkeypatch):
    """Below the hourly cap: schedule a delayed retry, do not exit."""
    exited = []
    monkeypatch.setattr(yuanbao.os, "_exit", lambda code: exited.append(code))

    cm = _connection()
    scheduled = []

    def _record(coro):
        coro.close()
        scheduled.append(1)

    cm._track_bg = _record  # type: ignore[assignment]

    cm._schedule_self_restart()
    assert scheduled == [1]
    assert exited == []
    assert len(cm._self_restart_times) == 1


@pytest.mark.asyncio
async def test_self_restart_bypasses_disconnected_running_guard():
    """After give-up, _running=False must not block the self-restart redial."""
    cm = _connection(reconnect={"self_restart_delay_s": 0.0})
    cm._adapter._running = False
    scheduled = []

    def _record(reason):
        scheduled.append(reason)

    cm._schedule_reconnect_task = _record  # type: ignore[assignment]

    cm._schedule_self_restart()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert scheduled == ["self_restart"]


def test_self_restart_over_limit_escalates_to_exit(monkeypatch):
    """Too many restarts within the hour escalate to a supervisor exit."""

    class _ExitCalled(Exception):
        def __init__(self, code):
            self.code = code

    def _fake_exit(code):
        raise _ExitCalled(code)

    monkeypatch.setattr(yuanbao.os, "_exit", _fake_exit)

    cm = _connection()
    now = time.monotonic()
    cm._self_restart_times = [now] * SELF_RESTART_MAX_PER_HOUR

    with pytest.raises(_ExitCalled) as ei:
        cm._schedule_self_restart()
    assert ei.value.code == GATEWAY_SERVICE_RESTART_EXIT_CODE


# --------------------------------------------------------------------------
# close() — background task teardown
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_cancels_and_clears_bg_tasks():
    """close() cancels watchdog/dormant tasks and drops their references."""
    cm = _connection()

    async def _forever():
        await asyncio.sleep(3600)

    watchdog = asyncio.create_task(_forever())
    dormant = asyncio.create_task(_forever())
    reconnect = asyncio.create_task(_forever())
    cm._watchdog_task = watchdog
    cm._dormant_task = dormant
    cm._reconnect_task = reconnect

    await cm.close()

    assert cm._stopping is True
    assert watchdog.cancelled()
    assert dormant.cancelled()
    assert reconnect.cancelled()
    assert cm._watchdog_task is None
    assert cm._dormant_task is None
    assert cm._reconnect_task is None
