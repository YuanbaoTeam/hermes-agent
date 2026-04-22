"""Tests for M2 development commands (/task) — AI 数字员工 研发闭环.

Covers:
- /task CommandDef registration with Development category
- Subcommand support (list / cancel / retry)
- GATEWAY_KNOWN_COMMANDS inclusion (dual-channel dispatch)
- Yuanbao group OwnerCommandMiddleware.ALLOWLIST inclusion
"""

from hermes_cli.commands import (
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    resolve_command,
)


M2_DEVELOPMENT_COMMANDS = ("task",)


class TestM2TaskCommandRegistry:
    def test_task_command_registered(self):
        names = {cmd.name for cmd in COMMAND_REGISTRY}
        assert "task" in names, "/task missing from COMMAND_REGISTRY"

    def test_task_uses_development_category(self):
        cmd = resolve_command("/task")
        assert cmd is not None
        assert cmd.category == "Development", f"/task should be 'Development', got '{cmd.category}'"

    def test_task_has_description_hint(self):
        cmd = resolve_command("/task")
        assert cmd.args_hint == "<description>"

    def test_task_has_subcommands(self):
        """/task list / cancel / retry subcommands are tab-completable."""
        cmd = resolve_command("/task")
        assert cmd.subcommands == ("list", "cancel", "retry")

    def test_task_is_not_cli_only(self):
        """/task must be available in BOTH CLI and gateway (dual-channel)."""
        cmd = resolve_command("/task")
        assert not cmd.cli_only
        assert not cmd.gateway_only


class TestM2DualChannelDispatch:
    def test_task_in_gateway_known_commands(self):
        assert "task" in GATEWAY_KNOWN_COMMANDS, (
            "/task missing from GATEWAY_KNOWN_COMMANDS — gateway dispatch won't route it"
        )


class TestM2YuanbaoGroupAllowlist:
    """Yuanbao group chat OwnerCommandMiddleware must allow /task without @Bot."""

    def test_allowlist_contains_task(self):
        try:
            from gateway.platforms.yuanbao import OwnerCommandMiddleware
        except ImportError:
            import pytest
            pytest.skip("Yuanbao platform adapter dependencies not available in this env")

        assert "/task" in OwnerCommandMiddleware.ALLOWLIST, (
            "/task missing from yuanbao OwnerCommandMiddleware.ALLOWLIST — "
            "owner will have to @Bot in group chat"
        )


class TestM2CategoryRegistryIsStable:
    """Belt-and-suspenders: ensure M1 knowledge commands still exist (no accidental removal)."""

    def test_m1_still_registered(self):
        for name in ("search", "note", "sync"):
            assert resolve_command("/" + name) is not None, f"M1 /{name} disappeared"

    def test_registry_grew_by_one_command(self):
        """M1 had 58 commands after landing; M2 added /task → 59; M4 added /optimize → 60."""
        # This is a soft guard — if we later add commands, update this test (or the M4 one).
        assert len(COMMAND_REGISTRY) == 60, (
            f"Unexpected command registry size: {len(COMMAND_REGISTRY)}. "
            "If you added a new command, update this test."
        )
