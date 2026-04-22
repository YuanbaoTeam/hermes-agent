"""Tests for M1 knowledge commands (/search /note /sync) — AI 数字员工 双通道支持.

Covers:
- Command registration in COMMAND_REGISTRY
- GATEWAY_KNOWN_COMMANDS inclusion (dual-channel dispatch)
- Yuanbao group OwnerCommandMiddleware.ALLOWLIST inclusion
"""

from hermes_cli.commands import (
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    resolve_command,
)


M1_KNOWLEDGE_COMMANDS = ("search", "note", "sync")


class TestM1KnowledgeCommandRegistry:
    """Each /search /note /sync must be a real CommandDef with the 'Knowledge' category."""

    def test_all_three_commands_registered(self):
        names = {cmd.name for cmd in COMMAND_REGISTRY}
        for cmd_name in M1_KNOWLEDGE_COMMANDS:
            assert cmd_name in names, f"/{cmd_name} missing from COMMAND_REGISTRY"

    def test_all_three_use_knowledge_category(self):
        for cmd_name in M1_KNOWLEDGE_COMMANDS:
            cmd = resolve_command("/" + cmd_name)
            assert cmd is not None
            assert cmd.category == "Knowledge", f"/{cmd_name} should be in 'Knowledge' category, got '{cmd.category}'"

    def test_search_has_query_hint(self):
        cmd = resolve_command("/search")
        assert cmd.args_hint == "<query>"

    def test_note_has_content_hint(self):
        cmd = resolve_command("/note")
        assert cmd.args_hint == "<content>"

    def test_sync_takes_no_args(self):
        cmd = resolve_command("/sync")
        assert cmd.args_hint == ""

    def test_none_are_cli_only(self):
        """Knowledge commands must be available in BOTH CLI and gateway (dual-channel M1)."""
        for cmd_name in M1_KNOWLEDGE_COMMANDS:
            cmd = resolve_command("/" + cmd_name)
            assert not cmd.cli_only, f"/{cmd_name} is cli_only — breaks dual-channel M1"
            assert not cmd.gateway_only, f"/{cmd_name} is gateway_only — unexpected"


class TestM1DualChannelDispatch:
    """Commands must be dispatch-able on both WeCom and Yuanbao gateways."""

    def test_all_three_in_gateway_known_commands(self):
        """GATEWAY_KNOWN_COMMANDS feeds gateway slash-command dispatch for every platform."""
        for cmd_name in M1_KNOWLEDGE_COMMANDS:
            assert cmd_name in GATEWAY_KNOWN_COMMANDS, (
                f"/{cmd_name} missing from GATEWAY_KNOWN_COMMANDS — "
                "gateway dispatch will fall through to default text handling"
            )


class TestYuanbaoGroupAllowlist:
    """Yuanbao group chat OwnerCommandMiddleware must allow /search /note /sync without @Bot."""

    def test_allowlist_contains_knowledge_commands(self):
        # Import here because loading yuanbao module has heavy deps (websockets)
        # and we only need the static ALLOWLIST attribute.
        try:
            from gateway.platforms.yuanbao import OwnerCommandMiddleware
        except ImportError:
            import pytest
            pytest.skip("Yuanbao platform adapter dependencies not available in this env")

        allowlist = OwnerCommandMiddleware.ALLOWLIST
        for cmd_name in M1_KNOWLEDGE_COMMANDS:
            assert "/" + cmd_name in allowlist, (
                f"/{cmd_name} missing from yuanbao OwnerCommandMiddleware.ALLOWLIST — "
                "owner will have to @Bot in group chat"
            )
