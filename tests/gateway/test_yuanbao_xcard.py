"""Behaviour contracts for Yuanbao xCard: wire envelope, validation, lifecycle, tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.platforms.yuanbao_xcard_protocol import (
    A2UI_VERSION,
    CATALOG_ID,
    EXT_CARD,
    EXT_DOWN,
    EXT_UP,
    card_messages_body,
    down_event_body,
    parse_up_event,
    wire_bytes,
)
from gateway.platforms.yuanbao_xcard_validate import (
    extract_actions,
    parse_generated_messages,
    render_generated_messages,
    validate_generated_messages,
)

CATALOG = CATALOG_ID


def _component_card() -> list[dict]:
    return [
        {
            "version": A2UI_VERSION,
            "createSurface": {"surfaceId": "draft", "catalogId": CATALOG},
        },
        {
            "version": A2UI_VERSION,
            "updateComponents": {
                "surfaceId": "draft",
                "components": [
                    {"id": "root", "component": "Card", "child": "label"},
                    {"id": "label", "component": "Text", "text": "Pick one"},
                    {
                        "id": "btn",
                        "component": "Button",
                        "child": "btn-label",
                        "action": {"event": {"name": "pick", "context": {"id": "a", "resultText": "已选择 A"}}},
                    },
                    {"id": "btn-label", "component": "Text", "text": "A"},
                ],
            },
        },
        {
            "version": A2UI_VERSION,
            "updateDataModel": {"surfaceId": "draft", "path": "/", "value": {"title": "Pick"}},
        },
    ]


def _envelope(extend_type: int, content) -> dict:
    text = json.dumps({"extend_type": extend_type, "content": content}, ensure_ascii=False)
    data = json.dumps({"elem_type": 1013, "text": text}, ensure_ascii=False)
    return {"msg_type": "TIMTextElem", "msg_content": {"data": data}}


def _up_payload(**overrides) -> dict:
    payload = {
        "event_id": "req-1",
        "surface_id": "surface-1",
        "event_name": "pick",
        "context_json": {"id": "a", "resultText": "已选择 A"},
        "revision": 1,
        "card_message_id": "im-9",
        "user_message": "已选择 A",
    }
    payload.update(overrides)
    return payload


class _Adapter:
    def __init__(self, *, enabled: bool = True):
        self._bot_id = "bot"
        self._xcard_enabled = enabled
        self._aio_loop = None
        self.sent: list[tuple[str, list]] = []
        self.wakes: list = []
        self.platform = None

    async def _xcard_send(self, group_code: str, body: list) -> dict:
        self.sent.append((group_code, body))
        return {"success": True, "msg_key": "m1"}

    def build_source(self, **kwargs):
        from gateway.config import Platform
        from gateway.session import SessionSource

        return SessionSource(
            platform=Platform.YUANBAO,
            chat_id=kwargs["chat_id"],
            chat_type=kwargs.get("chat_type") or "group",
            user_id=kwargs.get("user_id"),
            thread_id=kwargs.get("thread_id"),
        )

    async def handle_message(self, event) -> None:
        # admit_internal_event requires a concrete admission receipt, not a silent return.
        event._gateway_accepted = True
        self.wakes.append(event)


def _extend(body: list) -> dict:
    data = json.loads(body[0]["msg_content"]["data"])
    return json.loads(data["text"])


class TestWire:
    def test_markdown_1013_is_not_an_up_event(self):
        data = json.dumps({"elem_type": 1013, "text": "**hello**"})
        msg = [{"msg_type": "TIMCustomElem", "msg_content": {"data": data}}]
        assert parse_up_event(msg) is None

    def test_snake_case_click_normalizes(self):
        parsed = parse_up_event([_envelope(EXT_UP, _up_payload())])
        assert parsed["protocol"] == "yuanbao.openclaw.xcard/1"
        assert parsed["requestId"] == "req-1"
        assert parsed["surfaceId"] == "surface-1"
        assert parsed["binding"]["revision"] == 1
        assert parsed["binding"]["messageId"] == "im-9"
        assert parsed["event"]["action"]["name"] == "pick"
        assert parsed["event"]["action"]["context"]["resultText"] == "已选择 A"
        assert parsed["event"]["action"]["userMessage"] == "已选择 A"

    def test_card_body_is_extend_101_inside_elem_1013(self):
        body = card_messages_body([{"version": A2UI_VERSION, "createSurface": {"surfaceId": "s"}}])
        envelope = _extend(body)
        assert body[0]["msg_type"] == "TIMCustomElem"
        assert envelope["extend_type"] == EXT_CARD
        assert envelope["content"][0]["createSurface"]["surfaceId"] == "s"
        assert wire_bytes(body) > 0

    def test_down_event_carries_snapshot_without_being_a_new_card(self):
        body = down_event_body(
            card_message_id="im-9",
            surface_id="surface-1",
            revision=2,
            messages=[{"version": A2UI_VERSION, "updateDataModel": {"surfaceId": "surface-1", "value": {}}}],
        )
        envelope = _extend(body)
        assert envelope["extend_type"] == EXT_DOWN
        assert envelope["content"]["state_update"]["state_seq"] == 2
        assert envelope["content"]["card_message_id"] == "im-9"
        assert "createSurface" not in json.dumps(envelope["content"]["state_update"]["content"])


class TestValidate:
    def test_minimal_card_is_accepted_and_actions_are_extracted(self):
        messages = _component_card()
        result = validate_generated_messages(messages)
        assert result.ok, result.errors
        assert extract_actions(messages) == ["pick"]

    def test_webcard_and_missing_root_are_rejected(self):
        messages = _component_card()
        messages[1]["updateComponents"]["components"][0] = {"id": "root", "component": "WebCard"}
        result = validate_generated_messages(messages)
        assert not result.ok
        assert any("WebCard" in err or "root" in err for err in result.errors)

    def test_render_rewrites_surface_and_projects_openclaw_state(self):
        rendered = render_generated_messages(
            _component_card(),
            "real-id",
            {"revision": 2, "state": "active"},
            state="active",
            revision=2,
            last_result={"action": "pick", "context": {"resultText": "已选择 A"}, "userMessage": ""},
        )
        assert rendered[0]["createSurface"]["surfaceId"] == "real-id"
        assert rendered[-1]["updateDataModel"]["path"] == "/__openclaw"
        assert rendered[-1]["updateDataModel"]["value"]["resultText"] == "已选择 A"
        extension = rendered[0]["createSurface"]["metadata"]["extensions"]
        assert "com_tencent_yuanbao_openclaw" in extension

    def test_jsonl_parse(self):
        blob = "\n".join(json.dumps(m, ensure_ascii=False) for m in _component_card())
        parsed = parse_generated_messages(blob)
        assert "error" not in parsed
        assert len(parsed["messages"]) == 3


def _create_args(adapter):
    return dict(
        adapter=adapter,
        group_code="g1",
        session_key="sess",
        owner_user_id="alice",
        audience="requester",
        mode="once",
        title="Pick",
        fallback_text="reply with A or B",
        messages=_component_card(),
    )


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_create_sends_101_and_registers_surface(self):
        from gateway.platforms.yuanbao_xcard_service import create_generated_card

        adapter = _Adapter()
        rec = await create_generated_card(**_create_args(adapter))
        assert rec.state == "active"
        assert rec.revision == 1
        assert _extend(adapter.sent[0][1])["extend_type"] == EXT_CARD

    @pytest.mark.asyncio
    async def test_rejection_falls_back_once(self):
        from gateway.platforms.yuanbao_xcard_service import XCardSendError, create_generated_card

        adapter = _Adapter()

        async def reject(group_code, body):
            adapter.sent.append((group_code, body))
            if body[0]["msg_type"] != "TIMTextElem":
                return {"success": False, "error": "10002"}
            return {"success": True}

        adapter._xcard_send = reject
        with pytest.raises(XCardSendError) as exc:
            await create_generated_card(**_create_args(adapter))
        assert exc.value.fallback_sent is True
        assert adapter.sent[1][1][0]["msg_type"] == "TIMTextElem"
        with pytest.raises(XCardSendError) as again:
            await create_generated_card(**_create_args(adapter))
        assert again.value.fallback_sent is True
        assert len(adapter.sent) == 3  # second card rejected, fallback deduped

    @pytest.mark.asyncio
    async def test_rate_limit_does_not_fall_back(self):
        from gateway.platforms.yuanbao_xcard_service import XCardSendError, create_generated_card

        adapter = _Adapter()

        async def limited(group_code, body):
            adapter.sent.append((group_code, body))
            return {"success": True, "message": "bot rate limited"}

        adapter._xcard_send = limited
        with pytest.raises(XCardSendError) as exc:
            await create_generated_card(**_create_args(adapter))
        assert exc.value.kind == "rate_limited"
        assert exc.value.fallback_sent is False
        assert len(adapter.sent) == 1

    @pytest.mark.asyncio
    async def test_click_once_closes_and_wakes_when_nobody_waits(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event
        from gateway.platforms.yuanbao_xcard_registry import request_key
        from gateway.platforms.yuanbao_xcard_service import create_generated_card

        adapter = _Adapter()
        rec = await create_generated_card(**_create_args(adapter))
        payload = parse_up_event([_envelope(EXT_UP, _up_payload(surface_id=rec.surface_id))])
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        assert rec.state == "completed"
        assert rec.card_message_id == "im-9"
        assert rec.seen[request_key("alice", "req-1")]["status"] == "completed"
        kinds = [_extend(body)["extend_type"] for _, body in adapter.sent[1:]]
        assert kinds == [EXT_DOWN, EXT_DOWN]  # ack then snapshot
        snapshot = _extend(adapter.sent[-1][1])["content"]["state_update"]["content"]
        assert all("createSurface" not in message for message in snapshot)
        assert len(adapter.wakes) == 1
        assert "pick" in adapter.wakes[0].text
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        assert len(adapter.wakes) == 1

    @pytest.mark.asyncio
    async def test_waiter_consumes_click_without_a_second_turn(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event
        from gateway.platforms.yuanbao_xcard_registry import request_key, wait_for_surface_action
        from gateway.platforms.yuanbao_xcard_service import create_generated_card

        import asyncio

        adapter = _Adapter()
        rec = await create_generated_card(**{**_create_args(adapter), "mode": "perRevision"})
        task = asyncio.create_task(wait_for_surface_action(adapter, rec, timeout_s=5))
        await asyncio.wait({task}, timeout=0)
        payload = parse_up_event([_envelope(EXT_UP, _up_payload(surface_id=rec.surface_id, event_id="req-2"))])
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        outcome = await task
        assert outcome["status"] == "action"
        assert outcome["result"]["action"] == "pick"
        assert adapter.wakes == []
        assert rec.state == "active"
        assert rec.revision == 2
        assert rec.seen[request_key("alice", "req-2")]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_rejected_event_does_not_bind_card_message_id(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event
        from gateway.platforms.yuanbao_xcard_service import create_generated_card

        adapter = _Adapter()
        rec = await create_generated_card(**_create_args(adapter))
        assert rec.card_message_id == ""
        foreign = parse_up_event([
            _envelope(EXT_UP, _up_payload(
                surface_id=rec.surface_id, event_id="req-x", card_message_id="attacker-card",
            )),
        ])
        await handle_up_event(adapter, group_code="g1", operator_user_id="bob", payload=foreign)
        ack = _extend(adapter.sent[-1][1])["content"]["action_ack"]
        assert ack["success"] is False
        assert ack["error_code"] == "permission_denied"
        assert rec.card_message_id == ""
        accepted = parse_up_event([
            _envelope(EXT_UP, _up_payload(
                surface_id=rec.surface_id, event_id="req-ok", card_message_id="real-card",
            )),
        ])
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=accepted)
        assert rec.card_message_id == "real-card"
        down = _extend(adapter.sent[-1][1])["content"]
        assert down["card_message_id"] == "real-card"
        assert "state_update" in down

    @pytest.mark.asyncio
    async def test_other_user_and_bad_epoch_are_rejected(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event
        from gateway.platforms.yuanbao_xcard_service import create_generated_card

        adapter = _Adapter()
        rec = await create_generated_card(**_create_args(adapter))
        foreign = parse_up_event([
            _envelope(EXT_UP, _up_payload(surface_id=rec.surface_id, event_id="req-x")),
        ])
        await handle_up_event(adapter, group_code="g1", operator_user_id="bob", payload=foreign)
        ack = _extend(adapter.sent[-1][1])["content"]["action_ack"]
        assert ack["success"] is False
        assert ack["error_code"] == "permission_denied"
        assert rec.state == "active"
        stale_epoch = parse_up_event([
            _envelope(EXT_UP, _up_payload(surface_id=rec.surface_id, event_id="req-y", plugin_epoch="deadbeef")),
        ])
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=stale_epoch)
        ack = _extend(adapter.sent[-1][1])["content"]["action_ack"]
        assert ack["error_code"] == "epoch_expired"

    @pytest.mark.asyncio
    async def test_button_text_echo_is_consumed_once(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event
        from gateway.platforms.yuanbao_xcard_registry import consume_user_message_echo
        from gateway.platforms.yuanbao_xcard_service import create_generated_card

        adapter = _Adapter()
        rec = await create_generated_card(**_create_args(adapter))
        payload = parse_up_event([_envelope(EXT_UP, _up_payload(surface_id=rec.surface_id))])
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        assert consume_user_message_echo(adapter, "g1", "alice", "已选择 A") is True
        assert consume_user_message_echo(adapter, "g1", "alice", "已选择 A") is False


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_enabled_up_event_stops_the_pipeline(self):
        from gateway.platforms.yuanbao import InboundContext
        from gateway.platforms.yuanbao_xcard import XCardUpEventMiddleware

        adapter = _Adapter()
        ctx = InboundContext(adapter=adapter)
        ctx.chat_type = "group"
        ctx.group_code = "g1"
        ctx.from_account = "alice"
        ctx.msg_body = [_envelope(EXT_UP, _up_payload())]
        called = False

        async def _next():
            nonlocal called
            called = True

        await XCardUpEventMiddleware().handle(ctx, _next)
        assert called is False
        assert adapter.sent  # receipt for unknown surface

    @pytest.mark.asyncio
    async def test_disabled_flag_lets_the_message_through(self):
        from gateway.platforms.yuanbao import InboundContext
        from gateway.platforms.yuanbao_xcard import XCardUpEventMiddleware

        adapter = _Adapter(enabled=False)
        ctx = InboundContext(adapter=adapter)
        ctx.chat_type = "group"
        ctx.group_code = "g1"
        ctx.from_account = "alice"
        ctx.msg_body = [_envelope(EXT_UP, _up_payload())]
        called = False

        async def _next():
            nonlocal called
            called = True

        await XCardUpEventMiddleware().handle(ctx, _next)
        assert called is True
        assert adapter.sent == []

    def test_default_pipeline_intercepts_before_content_extract(self):
        from gateway.platforms.yuanbao import InboundPipelineBuilder

        names = InboundPipelineBuilder.build().middleware_names
        assert names.index("xcard-upevent") == names.index("extract-content") - 1


class TestTools:
    def _session(self, monkeypatch, *, chat_type="group", chat_id="group:g1", user_id="alice"):
        from gateway.session_context import set_session_vars

        set_session_vars(
            platform="yuanbao",
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            session_key="sess",
        )

    def test_send_refuses_dm_and_disabled_adapter(self, monkeypatch):
        import tools.yuanbao_xcard_tools as tools
        from tools.registry import registry

        adapter = _Adapter(enabled=False)
        monkeypatch.setattr(tools, "_adapter", lambda: adapter)
        self._session(monkeypatch, chat_type="dm", chat_id="direct:alice")
        raw = registry.dispatch("yb_xcard_send", {"messages": "[]", "title": "T", "mode": "once"})
        payload = json.loads(raw)
        assert payload["ok"] is False

        self._session(monkeypatch)
        raw = registry.dispatch(
            "yb_xcard_send",
            {"messages": json.dumps(_component_card()), "title": "Pick", "mode": "once"},
        )
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert payload["retryable"] is False

    def test_send_and_update_round_trip(self, monkeypatch):
        import tools.yuanbao_xcard_tools as tools
        from tools.registry import registry

        adapter = _Adapter()
        monkeypatch.setattr(tools, "_adapter", lambda: adapter)
        self._session(monkeypatch)
        raw = registry.dispatch(
            "yb_xcard_send",
            {
                "messages": json.dumps(_component_card()),
                "title": "Pick",
                "mode": "perRevision",
                "fallbackText": "text fallback",
            },
        )
        created = json.loads(raw)
        assert created["ok"] is True
        assert created["surfaceId"]
        updated_messages = _component_card()
        updated_messages[2]["updateDataModel"]["value"] = {"title": "Next"}
        raw = registry.dispatch(
            "yb_xcard_update",
            {"surfaceId": created["surfaceId"], "messages": json.dumps(updated_messages)},
        )
        updated = json.loads(raw)
        assert updated["ok"] is False  # no card_message_id until the client echoes one
        assert "msg_id" in updated["error"] or "消息" in updated["error"] or "card" in updated["error"].lower()
