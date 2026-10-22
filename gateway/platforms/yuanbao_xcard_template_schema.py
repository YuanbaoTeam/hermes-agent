"""Validation of model-supplied template data against a template's ``fields``.

Errors carry a JSON-ish path (``options[2].label``) so the tool can tell the model exactly
which field to fix. Light coercion only where models commonly slip: numbers/booleans given
as strings, numbers given for strings. Strings are trimmed; empty strings count as missing.
Unknown keys are dropped with a warning rather than failing the card. Over-long text is an
error (never silently truncated) so the model learns the limit.
"""

from __future__ import annotations

import json
import re
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def parse_template_data(raw: Any) -> dict:
    """Accept an object, or a JSON object string (the tool passes ``data`` as a string)."""
    value = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {"error": "data 为空"}
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return {"error": f"data 不是合法的 JSON: {exc}"}
    if not isinstance(value, dict):
        return {"error": "data 必须是 JSON 对象"}
    return {"data": value}


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _fail(errors: list, path: str, message: str):
    errors.append({"path": path, "message": message})


def _check_string(spec: dict, value: Any, path: str, errors: list) -> Any:
    text = str(value) if isinstance(value, (int, float, bool)) else value
    if not isinstance(text, str):
        return _fail(errors, path, "必须是字符串")
    text = text.strip()
    limit = spec.get("maxLength")
    if limit is not None and len(text) > limit:
        return _fail(errors, path, f"不能超过 {limit} 字（当前 {len(text)} 字）")
    if spec.get("enum") and text not in spec["enum"]:
        return _fail(errors, path, f"只能是 {' / '.join(spec['enum'])} 之一")
    if spec.get("format") == "id" and not _ID_RE.match(text):
        return _fail(errors, path, "只能包含字母、数字、下划线、横线，且不超过 32 个字符")
    if spec.get("format") == "url" and not _URL_RE.match(text):
        return _fail(errors, path, "必须是 http(s) 链接")
    return text


def _check_number(spec: dict, value: Any, path: str, errors: list) -> Any:
    try:
        number = float(value.strip()) if isinstance(value, str) and value.strip() else value
    except ValueError:
        return _fail(errors, path, "必须是数字")
    if isinstance(number, bool) or not isinstance(number, (int, float)) or number != number:
        return _fail(errors, path, "必须是数字")
    if spec.get("integer") and int(number) != number:
        return _fail(errors, path, "必须是整数")
    if spec.get("min") is not None and number < spec["min"]:
        return _fail(errors, path, f"不能小于 {spec['min']}")
    if spec.get("max") is not None and number > spec["max"]:
        return _fail(errors, path, f"不能大于 {spec['max']}")
    return int(number) if spec.get("integer") else number


def _check_array(spec: dict, value: Any, path: str, errors: list, warnings: list) -> Any:
    if not isinstance(value, list):
        return _fail(errors, path, "必须是数组")
    if spec.get("minItems") is not None and len(value) < spec["minItems"]:
        return _fail(errors, path, f"至少 {spec['minItems']} 项（当前 {len(value)} 项）")
    if spec.get("maxItems") is not None and len(value) > spec["maxItems"]:
        return _fail(errors, path, f"最多 {spec['maxItems']} 项（当前 {len(value)} 项）")
    return [_check_value(spec["items"], item, f"{path}[{i}]", errors, warnings) for i, item in enumerate(value)]


def _check_value(spec: dict, value: Any, path: str, errors: list, warnings: list) -> Any:
    if _is_missing(value):
        if spec.get("required"):
            return _fail(errors, path, "必填")
        return spec.get("default")
    kind = spec.get("type")
    if kind == "string":
        return _check_string(spec, value, path, errors)
    if kind == "number":
        return _check_number(spec, value, path, errors)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        return _fail(errors, path, "必须是 true 或 false")
    if kind == "array":
        return _check_array(spec, value, path, errors, warnings)
    if kind == "object":
        if not isinstance(value, dict):
            return _fail(errors, path, "必须是对象")
        return _check_object(spec["fields"], value, path, errors, warnings)
    return _fail(errors, path, f"未知字段类型 {kind}")


def _check_object(fields: dict, value: dict, path: str, errors: list, warnings: list) -> dict:
    out: dict[str, Any] = {}
    for key, spec in fields.items():
        checked = _check_value(spec, value.get(key), f"{path}.{key}" if path else key, errors, warnings)
        if checked is not None:
            out[key] = checked
    for key in value:
        if key not in fields:
            warnings.append(f"{path + '.' if path else ''}{key} 不是模板字段，已忽略")
    return out


def validate_template_data(fields: dict, data: dict) -> dict:
    errors: list = []
    warnings: list = []
    checked = _check_object(fields, data, "", errors, warnings)
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    return {"ok": True, "data": checked, "warnings": warnings}


def fill_ids(items: list, list_path: str, prefix: str) -> list[str]:
    """Fill missing ``id``s as ``<prefix><index+1>`` and report duplicates."""
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item.get("id"), str) or not item["id"]:
            item["id"] = f"{prefix}{index + 1}"
        if item["id"] in seen:
            errors.append(f"{list_path}[{index}].id: 与前面的项重复（{item['id']}）")
        seen.add(item["id"])
    return errors
