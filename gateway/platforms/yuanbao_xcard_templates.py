"""Built-in xCard templates: the model supplies data, never components.

Tool flow: list_templates → get_template (fields / events / example) → create with a
template key + data. ``prepare_template_data`` validates data (schema + template rules)
for both create and update. Each template turns validated data into A2UI components plus a
data model; ``decide_action`` maps a renderer event's raw context to the trusted
summary/context forwarded to the model, or rejects it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gateway.platforms.yuanbao_xcard_template_build import (
    BODY_ID,
    button,
    column,
    event,
    format_string,
    open_url,
    path,
    text,
    visible_if,
)
from gateway.platforms.yuanbao_xcard_template_schema import fill_ids, validate_template_data


@dataclass
class XCardTemplate:
    key: str
    name: str
    description: str
    usage: str
    mode: str  # once | perRevision
    fields: dict
    example: dict
    events: dict
    components: Callable[[dict], list]
    data_model: Callable[[dict, dict], dict]
    title: Callable[[dict], str]
    fallback_text: Callable[[dict], str]
    decide_action: Callable[[dict, str, dict], dict]
    result_text: Callable[[dict, dict], str]
    check: Optional[Callable[[dict], list]] = None


def _last_context(state: dict) -> dict:
    context = (state.get("last") or {}).get("context") or {}
    return context if isinstance(context, dict) else {}


# ---------------------------------------------------------------- choice

def _choice_selected_ids(state: dict) -> list[str]:
    context = _last_context(state)
    if isinstance(context.get("optionId"), str):
        return [context["optionId"]]
    ids = context.get("optionIds")
    return [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else []


def _choice_labels(data: dict, ids: list) -> list[str]:
    by_id = {o["id"]: o["label"] for o in data["options"]}
    return [by_id.get(i, i) for i in ids]


def _choice_components(data: dict) -> list:
    out = [text("title", path("/title"), "h4"), text("question", path("/question"), "body", "basic_text_secondary")]
    if not data.get("multiple"):
        out.insert(0, column(BODY_ID, ["title", "question", "options"]))
        out += [
            {"id": "options", "component": "List", "children": {"path": "/options", "componentId": "option"}, "style": {"spacing": 8}},
            {"id": "option", "component": "Button", "child": "option-body", "variant": "default",
             "action": event("select", {"optionId": path("id")}, format_string("我选了：${label}"))},
            column("option-body", ["option-label", "option-desc-vis"], 2),
            text("option-label", {"call": "select", "args": {
                "condition": {"call": "equalsString", "args": {"a": path("id"), "b": path("/state/selectedId")}},
                "then": format_string("✓ ${label}"), "else": path("label")}}),
            visible_if("option-desc-vis", "hasDescription", "option-desc"),
            text("option-desc", path("description"), "caption", "basic_text_tertiary"),
        ]
        return out
    out.insert(0, column(BODY_ID, ["title", "question", "picker", "submit"]))
    out.append({
        "id": "picker", "component": "ChoicePicker", "label": "可多选", "variant": "multipleSelection",
        "displayStyle": "chips",
        "options": [{"label": o["label"], "value": o["id"]} for o in data["options"]],
        "value": path("/state/selected"),
    })
    out += button("submit", data.get("submitLabel", "提交"),
                  event("submit", {"optionIds": path("/state/selected")}, "提交了选择"), "primary")
    return out


def _choice_model(data: dict, state: dict) -> dict:
    ids = _choice_selected_ids(state) if state.get("closed") else []
    return {
        "title": data["title"], "question": data["question"],
        "options": [{"id": o["id"], "label": o["label"], "description": o.get("description", ""),
                     "hasDescription": bool(o.get("description"))} for o in data["options"]],
        "state": {"selectedId": "" if data.get("multiple") else (ids[0] if ids else ""),
                  "selected": ids if data.get("multiple") else []},
    }


def _choice_fallback(data: dict) -> str:
    lines = [f"{i + 1}. {o['label']}" + (f"（{o['description']}）" if o.get("description") else "")
             for i, o in enumerate(data["options"])]
    return "\n".join([data["question"], *lines, "（可多选，请回复序号）" if data.get("multiple") else "（请回复序号）"])


def _choice_decide(data: dict, name: str, context: dict) -> dict:
    if name == "select" and not data.get("multiple"):
        option = next((o for o in data["options"] if o["id"] == context.get("optionId")), None)
        if option is None:
            return {"reject": "invalid_context"}
        return {"summary": f"选择了「{option['label']}」", "context": {"optionId": option["id"], "optionLabel": option["label"]}}
    if name == "submit" and data.get("multiple"):
        raw = context.get("optionIds") if isinstance(context.get("optionIds"), list) else []
        ids = [x for x in raw if isinstance(x, str)]
        known = {o["id"] for o in data["options"]}
        if any(i not in known for i in ids):
            return {"reject": "invalid_context"}
        if not ids:
            return {"summary": "没有勾选任何选项就点了提交", "context": {"optionIds": [], "optionLabels": []}, "keepOpen": True}
        labels = _choice_labels(data, ids)
        return {"summary": f"选择了{''.join(f'「{label}」' for label in labels)}",
                "context": {"optionIds": ids, "optionLabels": labels}}
    return {"reject": "invalid_action"}


CHOICE = XCardTemplate(
    key="choice", name="选择 / 投票",
    description="向用户提一个问题，给出 2~10 个选项让其单选或多选（方案选择、是否继续、偏好调查等）",
    usage="需要用户在有限选项中做决定时使用。单选时用户点一个选项即提交并关卡；multiple=true 时用户勾选多个后点提交。卡片发出后不要再用文字重复罗列选项。",
    mode="once",
    fields={
        "title": {"type": "string", "required": True, "maxLength": 40, "description": "卡片标题"},
        "question": {"type": "string", "required": True, "maxLength": 300, "description": "向用户提出的问题"},
        "options": {"type": "array", "required": True, "minItems": 2, "maxItems": 10, "description": "选项列表",
                    "items": {"type": "object", "description": "一个选项", "fields": {
                        "id": {"type": "string", "format": "id", "description": "选项标识（字母数字下划线横线），省略时自动生成 opt1、opt2…"},
                        "label": {"type": "string", "required": True, "maxLength": 40, "description": "选项文字"},
                        "description": {"type": "string", "maxLength": 80, "description": "选项补充说明（仅单选模式展示）"}}}},
        "multiple": {"type": "boolean", "default": False, "description": "是否多选"},
        "submitLabel": {"type": "string", "maxLength": 10, "default": "提交", "description": "多选模式的提交按钮文字"},
    },
    example={"title": "周五团建", "question": "这周五团建去哪儿？", "options": [
        {"id": "hotpot", "label": "火锅", "description": "人均 120"},
        {"id": "bbq", "label": "烧烤", "description": "人均 90"}, {"id": "ktv", "label": "KTV"}]},
    events={
        "select": {"description": "单选模式：用户点了一个选项（卡片随即关闭）",
                   "context": {"optionId": "选项 id", "optionLabel": "选项文字"}},
        "submit": {"description": "多选模式：用户点了提交（未选任何项时卡片保持打开）",
                   "context": {"optionIds": "选中的选项 id 数组", "optionLabels": "选中的选项文字数组"}},
    },
    components=_choice_components, data_model=_choice_model,
    title=lambda d: d["title"], fallback_text=_choice_fallback, decide_action=_choice_decide,
    result_text=lambda d, s: (f"已选择：{'、'.join(_choice_labels(d, _choice_selected_ids(s)))}"
                              if _choice_selected_ids(s) else ""),
    check=lambda d: fill_ids(d["options"], "options", "opt"),
)


# ---------------------------------------------------------------- confirm

def _confirm_components(data: dict) -> list:
    details = data.get("details") or []
    out = [
        column(BODY_ID, ["title", "content", *[f"d{i}" for i in range(len(details))], "buttons"], 10),
        text("title", path("/title"), "h4"), text("content", path("/content")),
    ]
    for i, row in enumerate(details):
        out += [
            {"id": f"d{i}", "component": "Row", "children": [f"d{i}-label", f"d{i}-value"], "align": "center", "style": {"spacing": 8}},
            text(f"d{i}-label", row["label"], "caption", "basic_text_secondary"),
            {**text(f"d{i}-value", row["value"]), "weight": 1},
        ]
    out.append({"id": "buttons", "component": "Row", "children": ["cancel", "confirm"], "style": {"spacing": 8}})
    out += button("cancel", data.get("cancelLabel", "取消"), event("cancel", {}, data.get("cancelLabel", "取消")))
    out += button("confirm", data.get("confirmLabel", "确认"), event("confirm", {}, data.get("confirmLabel", "确认")), "primary")
    return out


def _confirm_decide(data: dict, name: str, context: dict) -> dict:
    def said(label: str, meaning: str) -> str:
        return f"点了「{label}」" + ("" if label == meaning else f"（{meaning}）")

    if name == "confirm":
        return {"summary": said(data.get("confirmLabel", "确认"), "确认"), "context": {}}
    if name == "cancel":
        return {"summary": said(data.get("cancelLabel", "取消"), "取消"), "context": {}}
    return {"reject": "invalid_action"}


CONFIRM = XCardTemplate(
    key="confirm", name="确认卡",
    description="请用户确认或取消一件事（下单、执行操作、提交前确认等），可附带明细",
    usage="执行有影响的操作前需要用户明确同意时使用。用户点确认或取消后卡片关闭，事件名就是用户的答复。",
    mode="once",
    fields={
        "title": {"type": "string", "required": True, "maxLength": 40, "description": "卡片标题"},
        "content": {"type": "string", "required": True, "maxLength": 500, "description": "需要用户确认的内容"},
        "details": {"type": "array", "maxItems": 10, "description": "明细（可选），逐行展示「名称：值」",
                    "items": {"type": "object", "description": "一行明细", "fields": {
                        "label": {"type": "string", "required": True, "maxLength": 20, "description": "名称"},
                        "value": {"type": "string", "required": True, "maxLength": 100, "description": "值"}}}},
        "confirmLabel": {"type": "string", "maxLength": 8, "default": "确认", "description": "确认按钮文字"},
        "cancelLabel": {"type": "string", "maxLength": 8, "default": "取消", "description": "取消按钮文字"},
    },
    example={"title": "确认下单", "content": "将为你下单以下商品，确认后立即支付。",
             "details": [{"label": "商品", "value": "生椰拿铁（大杯 / 少冰）"},
                          {"label": "门店", "value": "科兴科学园店"}, {"label": "金额", "value": "¥18.00"}],
             "confirmLabel": "确认下单"},
    events={"confirm": {"description": "用户点了确认（卡片随即关闭）", "context": {}},
            "cancel": {"description": "用户点了取消（卡片随即关闭）", "context": {}}},
    components=_confirm_components,
    data_model=lambda d, s: {"title": d["title"], "content": d["content"]},
    title=lambda d: d["title"],
    fallback_text=lambda d: "\n".join([d["title"], d["content"],
                                       *[f"{r['label']}：{r['value']}" for r in d.get("details") or []],
                                       f"（请回复「{d.get('confirmLabel', '确认')}」或「{d.get('cancelLabel', '取消')}」）"]),
    decide_action=_confirm_decide,
    result_text=lambda d, s: (f"已{d.get('confirmLabel', '确认')}" if (s.get("last") or {}).get("action") == "confirm"
                              else f"已{d.get('cancelLabel', '取消')}" if (s.get("last") or {}).get("action") == "cancel" else ""),
)


# ---------------------------------------------------------------- form

_FIELD_TYPES = ["text", "number", "select", "multiselect", "checkbox", "date", "datetime"]


def _control_value(field: dict, value: Any) -> Any:
    kind = field["type"]
    if kind == "checkbox":
        return value is True or value == "true"
    if kind == "select":
        return [value] if isinstance(value, str) and value else []
    if kind == "multiselect":
        if isinstance(value, list):
            return value
        return [s.strip() for s in value.split(",") if s.strip()] if isinstance(value, str) and value else []
    if kind == "number":
        return str(value) if isinstance(value, (int, float)) else value if isinstance(value, str) else ""
    return value if isinstance(value, str) else ""


def _submitted_value(field: dict, value: Any) -> Any:
    kind = field["type"]
    if kind == "checkbox":
        return value is True or value == "true"
    if kind == "select":
        first = value[0] if isinstance(value, list) and value else value
        return first if isinstance(first, str) and first else None
    if kind == "multiselect":
        items = [x for x in value if isinstance(x, str)] if isinstance(value, list) else []
        return items or None
    if kind == "number":
        raw = str(value) if isinstance(value, (int, float)) else value.strip() if isinstance(value, str) else ""
        if not raw:
            return None
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return raw
    cleaned = value.strip() if isinstance(value, str) else ""
    return cleaned or None


def _display_value(field: dict, value: Any) -> str:
    if field["type"] == "checkbox":
        return "是" if value else "否"
    labels = {o["value"]: o["label"] for o in field.get("options") or []}
    if isinstance(value, list):
        return "、".join(labels.get(str(x), str(x)) for x in value)
    return labels.get(str(value), str(value)) if field["type"] == "select" else str(value)


def _form_check(data: dict) -> list:
    errors: list[str] = []
    keys: set[str] = set()
    for i, field in enumerate(data["fields"]):
        at = f"fields[{i}]"
        if field["key"] in keys:
            errors.append(f"{at}.key: 与前面的字段重复（{field['key']}）")
        keys.add(field["key"])
        choice = field["type"] in ("select", "multiselect")
        if choice and not field.get("options"):
            errors.append(f"{at}.options: {field['type']} 类型必须提供选项")
        if not choice and field.get("options"):
            field.pop("options", None)
        values = set()
        for j, option in enumerate(field.get("options") or []):
            if option["value"] in values:
                errors.append(f"{at}.options[{j}].value: 与前面的选项重复（{option['value']}）")
            values.add(option["value"])
        default = field.get("defaultValue")
        if default is None:
            continue
        if field["type"] == "number":
            try:
                float(default)
            except ValueError:
                errors.append(f"{at}.defaultValue: number 类型的默认值必须是数字")
        if field["type"] == "checkbox" and default not in ("true", "false"):
            errors.append(f"{at}.defaultValue: checkbox 默认值只能是 true / false")
        parts = ([s.strip() for s in default.split(",")] if field["type"] == "multiselect"
                 else [default] if field["type"] == "select" else [])
        for part in parts:
            if part and part not in values:
                errors.append(f"{at}.defaultValue: {part} 不在 options 的 value 中")
    return errors


def _form_components(data: dict) -> list:
    children = ["title"] + (["desc"] if data.get("description") else [])
    out = [text("title", path("/title"), "h4")]
    if data.get("description"):
        out.append(text("desc", path("/description"), "caption", "basic_text_secondary"))
    for i, field in enumerate(data["fields"]):
        cid = f"f{i}"
        label = field["label"] + (" *" if field.get("required") else "")
        value = path(f"/state/values/{field['key']}")
        children.append(cid)
        kind = field["type"]
        if kind in ("text", "number"):
            out += [column(cid, [f"{cid}-label", f"{cid}-input"], 4),
                    text(f"{cid}-label", label, "caption", "basic_text_secondary"),
                    {"id": f"{cid}-input", "component": "TextField",
                     "placeholder": field.get("placeholder") or f"请输入{field['label']}", "value": value}]
        elif kind in ("select", "multiselect"):
            out.append({"id": cid, "component": "ChoicePicker", "label": label,
                        "variant": "mutuallyExclusive" if kind == "select" else "multipleSelection",
                        "displayStyle": "chips",
                        "options": [{"label": o["label"], "value": o["value"]} for o in field.get("options") or []],
                        "value": value})
        elif kind == "checkbox":
            out.append({"id": cid, "component": "CheckBox", "label": label, "value": value})
        else:
            out.append({"id": cid, "component": "DateTimeInput", "label": label,
                        "enableDate": True, "enableTime": kind == "datetime", "value": value})
    children.append("submit")
    context = {f["key"]: path(f"/state/values/{f['key']}") for f in data["fields"]}
    out += button("submit", data.get("submitLabel", "提交"), event("submit", context, f"提交了「{data['title']}」"), "primary")
    out.insert(0, column(BODY_ID, children, 10))
    return out


def _form_model(data: dict, state: dict) -> dict:
    submitted = _last_context(state).get("values") if state.get("closed") else None
    submitted = submitted if isinstance(submitted, dict) else {}
    values = {f["key"]: _control_value(f, submitted[f["key"]] if f["key"] in submitted else f.get("defaultValue"))
              for f in data["fields"]}
    return {"title": data["title"], "description": data.get("description", ""), "state": {"values": values}}


def _form_decide(data: dict, name: str, context: dict) -> dict:
    if name != "submit":
        return {"reject": "invalid_action"}
    values: dict[str, Any] = {}
    missing: list[str] = []
    for field in data["fields"]:
        value = _submitted_value(field, context.get(field["key"]))
        if value is not None:
            values[field["key"]] = value
        if field.get("required") and (value is None or (field["type"] == "checkbox" and value is False)):
            missing.append(field["key"])
    shown = "；".join(f"{f['label']}={_display_value(f, values[f['key']])}" for f in data["fields"] if f["key"] in values)
    if missing:
        labels = [next((f["label"] for f in data["fields"] if f["key"] == k), k) for k in missing]
        return {"summary": f"提交了表单，但缺少必填项：{'、'.join(labels)}" + (f"（已填：{shown}）" if shown else ""),
                "context": {"values": values, "missing": missing}, "keepOpen": True}
    return {"summary": f"提交了表单{f'：{shown}' if shown else ''}", "context": {"values": values}}


FORM = XCardTemplate(
    key="form", name="表单收集",
    description="让用户填写 1~10 项信息后一次提交（报名、预约、问卷、收集参数等）",
    usage="需要用户一次性提供多项结构化信息时使用。每个字段用 key 标识，提交事件按 key 返回值；缺少必填项的提交也会转交给你（missing 列出缺项），卡片保持打开。",
    mode="once",
    fields={
        "title": {"type": "string", "required": True, "maxLength": 40, "description": "卡片标题"},
        "description": {"type": "string", "maxLength": 300, "description": "表单说明（可选）"},
        "fields": {"type": "array", "required": True, "minItems": 1, "maxItems": 10, "description": "表单字段",
                   "items": {"type": "object", "description": "一个字段", "fields": {
                       "key": {"type": "string", "required": True, "format": "id", "description": "字段标识，提交结果按它返回"},
                       "label": {"type": "string", "required": True, "maxLength": 20, "description": "字段名"},
                       "type": {"type": "string", "enum": _FIELD_TYPES, "default": "text",
                                "description": "text=文本 number=数字 select=单选 multiselect=多选 checkbox=勾选 date=日期 datetime=日期时间"},
                       "required": {"type": "boolean", "default": False, "description": "是否必填"},
                       "placeholder": {"type": "string", "maxLength": 40, "description": "输入提示（text/number）"},
                       "options": {"type": "array", "maxItems": 10, "description": "select/multiselect 的选项（必填）",
                                   "items": {"type": "object", "description": "一个选项", "fields": {
                                       "value": {"type": "string", "required": True, "format": "id", "description": "选项值"},
                                       "label": {"type": "string", "required": True, "maxLength": 20, "description": "选项文字"}}}},
                       "defaultValue": {"type": "string", "maxLength": 100,
                                        "description": "默认值（文本形式）：checkbox 填 true/false；select 填选项 value；multiselect 用英文逗号分隔多个 value；date 形如 2026-10-12；datetime 形如 2026-10-12T20:00"}}}},
        "submitLabel": {"type": "string", "maxLength": 10, "default": "提交", "description": "提交按钮文字"},
    },
    example={"title": "活动报名", "description": "周六上午 10 点开始，请填写报名信息", "fields": [
        {"key": "name", "label": "姓名", "type": "text", "required": True, "placeholder": "请输入姓名"},
        {"key": "people", "label": "人数", "type": "number", "defaultValue": "1"},
        {"key": "session", "label": "场次", "type": "select", "required": True,
         "options": [{"value": "am", "label": "上午"}, {"value": "pm", "label": "下午"}]},
        {"key": "date", "label": "日期", "type": "date"},
        {"key": "agree", "label": "同意活动须知", "type": "checkbox"}]},
    events={"submit": {"description": "用户点了提交", "context": {
        "values": "按字段 key 组织的提交值：text/date/datetime 为字符串，number 为数字，select 为选项 value，multiselect 为 value 数组，checkbox 为布尔",
        "missing": "（仅缺必填项时）缺少的字段 key 数组，此时卡片保持打开"}}},
    components=_form_components, data_model=_form_model, title=lambda d: d["title"],
    fallback_text=lambda d: "\n".join([d["title"], *([d["description"]] if d.get("description") else []),
                                       "请直接回复以下信息：",
                                       *[f"{i + 1}. {f['label']}{'（必填）' if f.get('required') else ''}"
                                         + (f"：{' / '.join(o['label'] for o in f['options'])}" if f.get("options") else "")
                                         for i, f in enumerate(d["fields"])]]),
    decide_action=_form_decide, result_text=lambda d, s: "已提交" if s.get("last") else "",
    check=_form_check,
)


# ---------------------------------------------------------------- info

def _info_components(data: dict) -> list:
    fields = data.get("fields") or []
    buttons = data.get("buttons") or []
    children = ["title"]
    out = [text("title", path("/title"), "h4")]
    if data.get("subtitle"):
        children.append("subtitle")
        out.append(text("subtitle", path("/subtitle"), "caption", "basic_text_secondary"))
    if data.get("imageUrl"):
        children.append("image")
        out.append({"id": "image", "component": "Image", "url": data["imageUrl"], "description": data["title"],
                    "fit": "cover", "style": {"fillMaxWidth": 1, "aspectRatio": 1.78, "shape": 8}})
    if data.get("content"):
        children.append("content")
        out.append(text("content", path("/content")))
    for i, row in enumerate(fields):
        children.append(f"f{i}")
        out += [{"id": f"f{i}", "component": "Row", "children": [f"f{i}-label", f"f{i}-value"], "align": "center", "style": {"spacing": 8}},
                text(f"f{i}-label", row["label"], "caption", "basic_text_secondary"),
                {**text(f"f{i}-value", row["value"]), "weight": 1}]
    if buttons:
        children.append("buttons")
        out.append({"id": "buttons", "component": "Row", "children": [f"b{i}" for i in range(len(buttons))], "style": {"spacing": 8}})
        for i, item in enumerate(buttons):
            action = open_url(item["url"]) if item.get("url") else event("click", {"buttonId": item["id"]}, item["label"])
            out += button(f"b{i}", item["label"], action, "primary" if i == 0 else "default")
    if data.get("footer"):
        children.append("footer")
        out.append(text("footer", path("/footer"), "caption", "basic_text_tertiary"))
    out.insert(0, column(BODY_ID, children, 8))
    return out


def _info_decide(data: dict, name: str, context: dict) -> dict:
    if name != "click":
        return {"reject": "invalid_action"}
    found = next((b for b in data.get("buttons") or [] if b["id"] == context.get("buttonId") and not b.get("url")), None)
    if found is None:
        return {"reject": "invalid_context"}
    return {"summary": f"点了按钮「{found['label']}」", "context": {"buttonId": found["id"], "buttonLabel": found["label"]}}


INFO = XCardTemplate(
    key="info", name="信息展示卡",
    description="结构化展示一段信息（摘要、通知、详情、天气、行程等），可带图片、字段列表和链接/操作按钮",
    usage="需要把信息排版得比纯文字更清晰时使用。按钮带 url 时点击直接打开链接；不带 url 时点击会以 click 事件转交给你。卡片不会因点击关闭，处理完可以 close 或 update。",
    mode="perRevision",
    fields={
        "title": {"type": "string", "required": True, "maxLength": 40, "description": "卡片标题"},
        "subtitle": {"type": "string", "maxLength": 60, "description": "副标题（可选）"},
        "imageUrl": {"type": "string", "format": "url", "description": "头图链接（可选，16:9 展示）"},
        "content": {"type": "string", "maxLength": 1000, "description": "正文（可选）"},
        "fields": {"type": "array", "maxItems": 12, "description": "字段列表（可选），逐行展示「名称：值」",
                   "items": {"type": "object", "description": "一行", "fields": {
                       "label": {"type": "string", "required": True, "maxLength": 20, "description": "名称"},
                       "value": {"type": "string", "required": True, "maxLength": 200, "description": "值"}}}},
        "buttons": {"type": "array", "maxItems": 4, "description": "按钮（可选）",
                    "items": {"type": "object", "description": "一个按钮", "fields": {
                        "id": {"type": "string", "format": "id", "description": "按钮标识，省略时自动生成 btn1、btn2…"},
                        "label": {"type": "string", "required": True, "maxLength": 10, "description": "按钮文字"},
                        "url": {"type": "string", "format": "url", "description": "点击打开的链接；省略则点击回传 click 事件"}}}},
        "footer": {"type": "string", "maxLength": 100, "description": "底部注释（可选），如数据来源、更新时间"},
    },
    example={"title": "深圳今日天气", "subtitle": "10 月 12 日 周一", "content": "多云转晴，午后有短时阵雨，出门记得带伞。",
             "fields": [{"label": "温度", "value": "24~31℃"}, {"label": "湿度", "value": "78%"}, {"label": "空气质量", "value": "良"}],
             "buttons": [{"id": "week", "label": "看未来一周"}, {"label": "气象台", "url": "https://weather.cma.cn"}],
             "footer": "数据来源：中国气象局"},
    events={"click": {"description": "用户点了一个不带 url 的按钮（卡片保持打开）",
                      "context": {"buttonId": "按钮 id", "buttonLabel": "按钮文字"}}},
    components=_info_components,
    data_model=lambda d, s: {"title": d["title"], "subtitle": d.get("subtitle", ""),
                             "content": d.get("content", ""), "footer": d.get("footer", "")},
    title=lambda d: d["title"],
    fallback_text=lambda d: "\n".join([d["title"], *([d["subtitle"]] if d.get("subtitle") else []),
                                       *([d["content"]] if d.get("content") else []),
                                       *[f"{r['label']}：{r['value']}" for r in d.get("fields") or []],
                                       *[f"{b['label']}：{b['url']}" for b in d.get("buttons") or [] if b.get("url")],
                                       *([d["footer"]] if d.get("footer") else [])]),
    decide_action=_info_decide, result_text=lambda d, s: "",
    check=lambda d: fill_ids(d["buttons"], "buttons", "btn") if d.get("buttons") else [],
)


# ---------------------------------------------------------------- item_list

def _meta_text(item: dict) -> str:
    return " · ".join(x for x in [item.get("price", ""), *(item.get("tags") or [])] if x)


def _item_list_components(data: dict) -> list:
    out = [
        column(BODY_ID, ["title", *(["subtitle"] if data.get("subtitle") else []), "items"]),
        text("title", path("/title"), "h4"),
        {"id": "items", "component": "List", "children": {"path": "/items", "componentId": "item"}, "style": {"spacing": 10}},
        {"id": "item", "component": "Row", "children": ["item-img-vis", "item-main"], "align": "start", "style": {"spacing": 10}},
        visible_if("item-img-vis", "hasImage", "item-img"),
        {"id": "item-img", "component": "Image", "url": path("imageUrl"), "description": "图片", "fit": "cover",
         "style": {"width": 56, "height": 56, "shape": 8}},
        {**column("item-main", ["item-title", "item-sub-vis", "item-desc-vis", "item-meta-vis",
                                *(["item-btn"] if data.get("selectable", True) else [])], 4),
         "align": "start", "weight": 1},
        text("item-title", path("title"), "h5"),
        visible_if("item-sub-vis", "hasSubtitle", "item-sub"),
        text("item-sub", path("subtitle"), "caption", "basic_text_secondary"),
        visible_if("item-desc-vis", "hasDescription", "item-desc"),
        text("item-desc", path("description"), "caption", "basic_text_tertiary"),
        visible_if("item-meta-vis", "hasMeta", "item-meta"),
        text("item-meta", path("metaText"), "caption", "basic_text_brand"),
    ]
    if data.get("subtitle"):
        out.append(text("subtitle", path("/subtitle"), "caption", "basic_text_secondary"))
    if data.get("selectable", True):
        out += [
            {"id": "item-btn", "component": "Button", "child": "item-btn-label", "variant": "default",
             "action": event("select", {"itemId": path("id")}, format_string("我选了：${title}"))},
            text("item-btn-label", {"call": "select", "args": {
                "condition": {"call": "equalsString", "args": {"a": path("id"), "b": path("/state/selectedId")}},
                "then": "✓ 已选", "else": data.get("actionLabel", "选择")}}),
        ]
    return out


def _item_list_model(data: dict, state: dict) -> dict:
    selected = _last_context(state).get("itemId") if state.get("closed") else ""
    return {
        "title": data["title"], "subtitle": data.get("subtitle", ""),
        "items": [{"id": it["id"], "title": it["title"], "subtitle": it.get("subtitle", ""),
                   "description": it.get("description", ""), "imageUrl": it.get("imageUrl", ""),
                   "metaText": _meta_text(it), "hasImage": bool(it.get("imageUrl")),
                   "hasSubtitle": bool(it.get("subtitle")), "hasDescription": bool(it.get("description")),
                   "hasMeta": bool(_meta_text(it))} for it in data["items"]],
        "state": {"selectedId": selected if isinstance(selected, str) else ""},
    }


def _item_list_decide(data: dict, name: str, context: dict) -> dict:
    if name != "select" or not data.get("selectable", True):
        return {"reject": "invalid_action"}
    item = next((it for it in data["items"] if it["id"] == context.get("itemId")), None)
    if item is None:
        return {"reject": "invalid_context"}
    return {"summary": f"选择了「{item['title']}」", "context": {"itemId": item["id"], "itemTitle": item["title"]}}


ITEM_LIST = XCardTemplate(
    key="item_list", name="商品 / 门店列表",
    description="展示 1~10 个商品、门店或搜索结果（图片、标题、副标题、价格、标签），可让用户点选其中一个",
    usage="给用户看一组候选对象并让其挑一个时使用（selectable=false 时仅展示）。图片必须是可公开访问的 http(s) 链接，没有就省略 imageUrl。",
    mode="once",
    fields={
        "title": {"type": "string", "required": True, "maxLength": 40, "description": "卡片标题"},
        "subtitle": {"type": "string", "maxLength": 60, "description": "标题下的说明（可选）"},
        "items": {"type": "array", "required": True, "minItems": 1, "maxItems": 10, "description": "列表项",
                  "items": {"type": "object", "description": "一项", "fields": {
                      "id": {"type": "string", "format": "id", "description": "项标识，省略时自动生成 item1、item2…"},
                      "title": {"type": "string", "required": True, "maxLength": 40, "description": "名称"},
                      "subtitle": {"type": "string", "maxLength": 60, "description": "副标题，如地址、规格"},
                      "description": {"type": "string", "maxLength": 120, "description": "补充描述，如距离、营业时间"},
                      "imageUrl": {"type": "string", "format": "url", "description": "缩略图链接"},
                      "price": {"type": "string", "maxLength": 20, "description": "价格文本，如 ¥18、¥32 起"},
                      "tags": {"type": "array", "maxItems": 3, "description": "标签",
                               "items": {"type": "string", "required": True, "maxLength": 10, "description": "标签文字"}}}}},
        "actionLabel": {"type": "string", "maxLength": 8, "default": "选择", "description": "每项按钮文字"},
        "selectable": {"type": "boolean", "default": True, "description": "是否可点选；false 时只展示"},
    },
    example={"title": "附近门店", "subtitle": "按距离排序", "items": [
        {"id": "s1", "title": "科兴科学园店", "subtitle": "南山区科苑路 15 号", "description": "距你 350m · 营业至 22:00", "tags": ["可自提"]},
        {"id": "s2", "title": "腾讯滨海大厦店", "subtitle": "南山区后海大道 33 号", "description": "距你 1.2km", "tags": ["可外送", "人少"]}],
        "actionLabel": "去这家"},
    events={"select": {"description": "用户点选了某一项（卡片随即关闭）",
                       "context": {"itemId": "项 id", "itemTitle": "项名称"}}},
    components=_item_list_components, data_model=_item_list_model, title=lambda d: d["title"],
    fallback_text=lambda d: "\n".join([d["title"], *([d["subtitle"]] if d.get("subtitle") else []),
                                       *[f"{i + 1}. {it['title']}"
                                         + (f"（{'，'.join(x for x in [it.get('subtitle', ''), it.get('description', ''), _meta_text(it)] if x)}）"
                                            if any([it.get("subtitle"), it.get("description"), _meta_text(it)]) else "")
                                         for i, it in enumerate(d["items"])],
                                       *(["（请回复序号选择）"] if d.get("selectable", True) else [])]),
    decide_action=_item_list_decide,
    result_text=lambda d, s: (f"已选择：{next((it['title'] for it in d['items'] if it['id'] == _last_context(s).get('itemId')), '')}"
                              if next((it for it in d["items"] if it["id"] == _last_context(s).get("itemId")), None) else ""),
    check=lambda d: fill_ids(d["items"], "items", "item"),
)


# ---------------------------------------------------------------- registry

TEMPLATES: tuple = (CHOICE, FORM, ITEM_LIST, CONFIRM, INFO)
_BY_KEY = {tpl.key: tpl for tpl in TEMPLATES}


def get_template(key: str) -> Optional[XCardTemplate]:
    return _BY_KEY.get(key or "")


def list_templates() -> list:
    return [{"key": t.key, "name": t.name, "description": t.description} for t in TEMPLATES]


def describe_template(tpl: XCardTemplate) -> dict:
    return {
        "key": tpl.key, "name": tpl.name, "description": tpl.description, "usage": tpl.usage,
        "closesAfterAction": tpl.mode == "once",
        "fields": tpl.fields, "events": tpl.events, "example": tpl.example,
    }


def prepare_template_data(tpl: XCardTemplate, data: dict) -> dict:
    """Schema validation plus the template's cross-field rules (which may fill defaults in place)."""
    checked = validate_template_data(tpl.fields, copy.deepcopy(data))
    if not checked["ok"]:
        return checked
    prepared = checked["data"]
    errors = []
    if tpl.check is not None:
        for message in tpl.check(prepared):
            head, sep, tail = message.partition(": ")
            errors.append({"path": head if sep else "", "message": tail if sep else message})
    if errors:
        return {"ok": False, "errors": errors, "warnings": checked["warnings"]}
    return {"ok": True, "data": prepared, "warnings": checked["warnings"]}
