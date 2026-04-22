"""Tests for M4 /optimize command — AI 数字员工 autoresearch.

Covers command registration, subcommand routing surface, and Yuanbao allowlist.
Runner internals are tested in tests/agent/test_autoresearch.py.
"""
from hermes_cli.commands import (
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    resolve_command,
)


class TestM4OptimizeCommandRegistry:
    def test_optimize_registered(self):
        names = {cmd.name for cmd in COMMAND_REGISTRY}
        assert "optimize" in names, "/optimize missing from COMMAND_REGISTRY"

    def test_optimize_uses_development_category(self):
        cmd = resolve_command("/optimize")
        assert cmd is not None
        assert cmd.category == "Development"

    def test_optimize_has_skill_name_hint(self):
        cmd = resolve_command("/optimize")
        assert cmd.args_hint == "<skill-name>"

    def test_optimize_has_subcommands(self):
        cmd = resolve_command("/optimize")
        assert cmd.subcommands == ("list", "cancel", "status")

    def test_optimize_dual_channel(self):
        cmd = resolve_command("/optimize")
        assert not cmd.cli_only
        assert not cmd.gateway_only


class TestM4DualChannelDispatch:
    def test_optimize_in_gateway_known_commands(self):
        assert "optimize" in GATEWAY_KNOWN_COMMANDS


class TestM4YuanbaoAllowlist:
    def test_allowlist_contains_optimize(self):
        try:
            from gateway.platforms.yuanbao import OwnerCommandMiddleware
        except ImportError:
            import pytest
            pytest.skip("Yuanbao platform adapter not available")
        assert "/optimize" in OwnerCommandMiddleware.ALLOWLIST


class TestM4RegistryGrowthSoftGuard:
    """M3 had 59; M4 adds /optimize → 60."""

    def test_registry_size_after_m4(self):
        assert len(COMMAND_REGISTRY) == 60, (
            f"Unexpected command registry size: {len(COMMAND_REGISTRY)}. "
            "If you added a new command, update this test."
        )

    def test_all_prior_milestones_intact(self):
        for name in ("search", "note", "sync", "task", "optimize"):
            assert resolve_command("/" + name) is not None
