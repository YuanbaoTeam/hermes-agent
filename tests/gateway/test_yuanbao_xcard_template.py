"""Behaviour contracts for built-in xCard templates: data validation, rendering, clicks, tool."""

from __future__ import annotations

import json

import pytest

from gateway.platforms.yuanbao_xcard_protocol import EXT_CARD, EXT_DOWN, EXT_UP, parse_up_event
from gateway.platforms.yuanbao_xcard_validate import validate_a2ui_messages


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
        event._gateway_accepted = True
        self.wakes.append(event)


def _extend(body: list) -> dict:
    data = json.loads(body[0]["msg_content"]["data"])
    return json.loads(data["text"])


def _envelope(extend_type: int, content) -> dict:
    text = json.dumps({"extend_type": extend_type, "content": content}, ensure_ascii=False)
    data = json.dumps({"elem_type": 1013, "text": text}, ensure_ascii=False)
    return {"msg_type": "TIMTextElem", "msg_content": {"data": data}}


def _click(adapter, rec, *, name, context, request_id="req-1", operator="alice"):
    payload = parse_up_event([_envelope(EXT_UP, {
        "event_id": request_id,
        "surface_id": rec.surface_id,
        "event_name": name,
        "context_json": context,
        "revision": rec.revision,
        "card_message_id": "im-9",
        "user_message": "clicked",
    })])
    return payload


async def _create(adapter, key, data):
    from gateway.platforms.yuanbao_xcard_service import create_template_card
    from gateway.platforms.yuanbao_xcard_templates import get_template, prepare_template_data

    tpl = get_template(key)
    prepared = prepare_template_data(tpl, data)
    assert prepared["ok"], prepared
    return await create_template_card(
        adapter, group_code="g1", session_key="sess", owner_user_id="alice",
        audience="requester", tpl=tpl, data=prepared["data"],
    )


CHOICE_DATA = {
    "title": "团建", "question": "去哪儿？",
    "options": [{"id": "hotpot", "label": "火锅"}, {"id": "bbq", "label": "烧烤"}],
}


class TestCatalog:
    def test_five_templates_are_listed_and_described(self):
        from gateway.platforms.yuanbao_xcard_templates import describe_template, get_template, list_templates

        keys = [item["key"] for item in list_templates()]
        assert keys == ["choice", "form", "item_list", "confirm", "info"]
        described = describe_template(get_template("choice"))
        assert described["fields"]["options"]["minItems"] == 2
        assert "select" in described["events"]
        assert described["example"]["options"]

    def test_every_example_renders_a_valid_card(self):
        from gateway.platforms.yuanbao_xcard_service import render_messages
        from gateway.platforms.yuanbao_xcard_registry import Surface
        from gateway.platforms.yuanbao_xcard_templates import TEMPLATES, prepare_template_data

        for tpl in TEMPLATES:
            prepared = prepare_template_data(tpl, json.loads(json.dumps(tpl.example)))
            assert prepared["ok"], (tpl.key, prepared)
            rec = Surface(
                surface_id="s1", group_code="g1", session_key="sess", owner_user_id="alice",
                audience="requester", mode=tpl.mode, title=tpl.title(prepared["data"]),
                fallback_text=tpl.fallback_text(prepared["data"]), messages=[],
                actions=list(tpl.events), kind="template", template=tpl.key, data=prepared["data"],
            )
            checked = validate_a2ui_messages(render_messages(rec))
            assert checked.ok, (tpl.key, checked.errors)

    def test_invalid_data_names_the_field(self):
        from gateway.platforms.yuanbao_xcard_templates import get_template, prepare_template_data

        prepared = prepare_template_data(get_template("choice"), {"title": "T", "question": "Q", "options": [{"label": "only"}]})
        assert not prepared["ok"]
        assert any("options" in err["path"] for err in prepared["errors"])


class TestTemplateLifecycle:
    @pytest.mark.asyncio
    async def test_create_sends_template_card(self):
        adapter = _Adapter()
        rec = await _create(adapter, "choice", CHOICE_DATA)
        assert rec.kind == "template" and rec.template == "choice"
        envelope = _extend(adapter.sent[0][1])
        assert envelope["extend_type"] == EXT_CARD
        create = envelope["content"][0]["createSurface"]
        extension = create["metadata"]["extensions"]["com_tencent_yuanbao_openclaw"]
        assert extension["source"] == "template" and extension["template"] == "choice"

    @pytest.mark.asyncio
    async def test_choice_click_forwards_trusted_context_and_closes(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event

        adapter = _Adapter()
        rec = await _create(adapter, "choice", CHOICE_DATA)
        payload = _click(adapter, rec, name="select", context={"optionId": "bbq", "optionLabel": "伪造"})
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        assert rec.state == "completed"
        assert len(adapter.wakes) == 1
        assert "烧烤" in adapter.wakes[0].text
        assert "伪造" not in adapter.wakes[0].text
        assert "yb_xcard_template" not in adapter.wakes[0].text  # closed card: no update hint needed

    @pytest.mark.asyncio
    async def test_unknown_option_is_rejected_and_card_stays_open(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event

        adapter = _Adapter()
        rec = await _create(adapter, "choice", CHOICE_DATA)
        payload = _click(adapter, rec, name="select", context={"optionId": "nope"})
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        ack = _extend(adapter.sent[-1][1])["content"]["action_ack"]
        assert ack["success"] is False
        assert ack["error_code"] == "invalid_context"
        assert rec.state == "active"
        assert adapter.wakes == []

    @pytest.mark.asyncio
    async def test_form_missing_required_keeps_card_open(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event

        adapter = _Adapter()
        rec = await _create(adapter, "form", {
            "title": "报名",
            "fields": [{"key": "name", "label": "姓名", "type": "text", "required": True}],
        })
        payload = _click(adapter, rec, name="submit", context={"name": ""})
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice", payload=payload)
        assert rec.state == "active"
        assert len(adapter.wakes) == 1
        assert '"missing": ["name"]' in adapter.wakes[0].text or '"missing":["name"]' in adapter.wakes[0].text.replace(" ", "")
        assert rec.revision == 2  # re-rendered so the same revision cannot be consumed twice

    @pytest.mark.asyncio
    async def test_info_click_stays_open_for_another_click(self):
        from gateway.platforms.yuanbao_xcard_events import handle_up_event

        adapter = _Adapter()
        rec = await _create(adapter, "info", {
            "title": "天气", "content": "晴",
            "buttons": [{"id": "week", "label": "看一周"}],
        })
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice",
                              payload=_click(adapter, rec, name="click", context={"buttonId": "week"}, request_id="r1"))
        assert rec.state == "active"
        await handle_up_event(adapter, group_code="g1", operator_user_id="alice",
                              payload=_click(adapter, rec, name="click", context={"buttonId": "week"}, request_id="r2"))
        assert len(adapter.wakes) == 2
        assert "yb_xcard_template" in adapter.wakes[0].text


class TestTemplateTool:
    def _session(self):
        from gateway.session_context import set_session_vars

        set_session_vars(platform="yuanbao", chat_id="group:g1", chat_type="group",
                         user_id="alice", session_key="sess")

    def test_list_and_get_template_need_no_card(self):
        import tools.yuanbao_xcard_template_tool as _tool_mod  # registration side effect
        from tools.registry import registry

        listed = json.loads(registry.dispatch("yb_xcard_template", {"action": "list_templates"}))
        assert listed["ok"] is True
        assert {item["key"] for item in listed["templates"]} >= {"choice", "form"}
        described = json.loads(registry.dispatch(
            "yb_xcard_template", {"action": "get_template", "template": "form"}))
        assert described["ok"] is True
        assert "fields" in described and "example" in described
        unknown = json.loads(registry.dispatch(
            "yb_xcard_template", {"action": "get_template", "template": "nope"}))
        assert unknown["ok"] is False and unknown["templates"]

    def test_create_update_close_get_round_trip(self, monkeypatch):
        import tools.yuanbao_xcard_tools as xcard_tools
        from gateway.platforms.yuanbao_xcard_registry import get_surface
        from tools.registry import registry

        adapter = _Adapter()
        monkeypatch.setattr(xcard_tools, "_adapter", lambda: adapter)
        self._session()
        created = json.loads(registry.dispatch("yb_xcard_template", {
            "action": "create", "template": "choice", "data": json.dumps(CHOICE_DATA, ensure_ascii=False),
        }))
        assert created["ok"] is True, created
        rec = get_surface(adapter, created["surfaceId"])
        rec.card_message_id = "im-9"  # the client echoes the id on the first click

        updated = json.loads(registry.dispatch("yb_xcard_template", {
            "action": "update", "surfaceId": rec.surface_id,
            "data": json.dumps({"question": "改去哪儿？"}, ensure_ascii=False),
        }))
        assert updated["ok"] is True, updated
        assert updated["revision"] == 2
        kinds = [_extend(body)["extend_type"] for _, body in adapter.sent]
        assert kinds[-1] == EXT_DOWN

        got = json.loads(registry.dispatch("yb_xcard_template", {"action": "get", "surfaceId": rec.surface_id}))
        assert got["ok"] is True and got["state"] == "active"

        closed = json.loads(registry.dispatch("yb_xcard_template", {"action": "close", "surfaceId": rec.surface_id}))
        assert closed["ok"] is True and closed["state"] == "cancelled"

    def test_create_with_bad_data_returns_field_errors(self, monkeypatch):
        import tools.yuanbao_xcard_tools as xcard_tools
        from tools.registry import registry

        adapter = _Adapter()
        monkeypatch.setattr(xcard_tools, "_adapter", lambda: adapter)
        self._session()
        refused = json.loads(registry.dispatch("yb_xcard_template", {
            "action": "create", "template": "choice",
            "data": json.dumps({"title": "T", "question": "Q", "options": [{"label": "only"}]}),
        }))
        assert refused["ok"] is False
        assert refused["retryable"] is True
        assert any("options" in err for err in refused["errors"])
        assert adapter.sent == []
