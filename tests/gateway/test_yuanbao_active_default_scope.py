"""Default-profile adapter lookup from inside a profile runtime scope.

Regression: modern gateways always install ``session_authority``, so primary turns run inside
``_profile_runtime_scope(<profile home>)`` (same as ``multiplex_profiles``). That sets
``get_hermes_home_override()`` even for the DEFAULT profile. Startup, however, connects the
default adapter with NO override, so ``set_active`` stores it in the unscoped
``_active_instance`` slot. A dict-only lookup then returned ``None`` and every Yuanbao tool
(``yb_xcard_send``, ``yb_send_dm``, ...) failed with "Yuanbao adapter is not connected" while
the bot was demonstrably online.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from gateway.platforms.yuanbao import YuanbaoAdapter
from hermes_constants import (
    get_default_hermes_root,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@pytest.fixture
def clean_slots():
    prev_instance = YuanbaoAdapter._active_instance
    prev_map = dict(YuanbaoAdapter._active_instances)
    YuanbaoAdapter._active_instance = None
    YuanbaoAdapter._active_instances = {}
    try:
        yield
    finally:
        YuanbaoAdapter._active_instance = prev_instance
        YuanbaoAdapter._active_instances = prev_map


def test_default_profile_scope_sees_unscoped_adapter(clean_slots):
    """Registered unscoped (startup), looked up under the default root's scope."""
    adapter = object()
    YuanbaoAdapter.set_active(adapter)  # no override: startup path
    assert YuanbaoAdapter._active_instance is adapter

    token = set_hermes_home_override(str(get_default_hermes_root()))
    try:
        assert YuanbaoAdapter.get_active() is adapter
    finally:
        reset_hermes_home_override(token)


def test_secondary_profile_scope_does_not_fall_back(clean_slots, tmp_path):
    """A secondary profile must never route through the default profile's bot."""
    adapter = object()
    YuanbaoAdapter.set_active(adapter)

    secondary = tmp_path / "profiles" / "harmony-dev"
    secondary.mkdir(parents=True)
    token = set_hermes_home_override(str(secondary))
    try:
        assert YuanbaoAdapter.get_active() is None
    finally:
        reset_hermes_home_override(token)


def test_scoped_registration_still_wins(clean_slots, tmp_path):
    """An adapter registered under a profile scope is returned for that scope."""
    default_adapter, scoped_adapter = object(), object()
    YuanbaoAdapter.set_active(default_adapter)

    secondary = tmp_path / "profiles" / "beta"
    secondary.mkdir(parents=True)
    token = set_hermes_home_override(str(secondary))
    try:
        YuanbaoAdapter.set_active(scoped_adapter)
        assert YuanbaoAdapter.get_active() is scoped_adapter
    finally:
        reset_hermes_home_override(token)

    assert YuanbaoAdapter._active_instance is default_adapter
