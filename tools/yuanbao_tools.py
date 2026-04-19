"""
yuanbao_tools.py - 元宝平台工具集

提供以下工具函数，供 hermes-agent 的 "hermes-yuanbao" toolset 使用：
  - get_group_info        : 查询群基本信息（群名、群主、成员数）
  - query_group_members   : 查询群成员（按名搜索、列举 bot、列举全部）
  - search_sticker        : 按关键词搜索内置贴纸（返回候选列表，含 sticker_id/name/description）
  - send_sticker          : 向当前会话或指定 chat_id 发送贴纸（TIMFaceElem）

对齐 chatbot-web/yuanbao-openclaw-plugin 的 sticker-search/sticker-send 行为：
LLM 应先用 search_sticker 找到合适的 sticker_id（或直接传中文 name），再用 send_sticker
发送。不要在文本中夹杂裸的 Unicode emoji 当作贴纸。

The active adapter singleton lives in ``gateway.platforms.yuanbao`` and is
accessed via ``get_active_adapter()``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)


def _get_active_adapter():
    """Lazy import to avoid ImportError when gateway.platforms.yuanbao is unavailable."""
    try:
        from gateway.platforms.yuanbao import get_active_adapter
        return get_active_adapter()
    except ImportError:
        return None


if TYPE_CHECKING:
    from gateway.platforms.yuanbao import YuanbaoAdapter


# ---------------------------------------------------------------------------
# 角色标签
# ---------------------------------------------------------------------------

_USER_TYPE_LABEL = {0: "unknown", 1: "user", 2: "yuanbao_ai", 3: "bot"}

MENTION_HINT = (
    'To @mention a user, you MUST use the format: '
    'space + @ + nickname + space (e.g. " @Alice ").'
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

async def get_group_info(group_code: str) -> dict:
    """查询群基本信息（群名、群主、成员数）。"""
    if not group_code:
        return {"success": False, "error": "group_code is required"}

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    try:
        gi = await adapter.query_group_info(group_code)
        if gi is None:
            return {"success": False, "error": "query_group_info returned None"}
        return {
            "success": True,
            "group_code": group_code,
            "group_name": gi.get("group_name", ""),
            "member_count": gi.get("member_count", 0),
            "owner": {
                "user_id": gi.get("owner_id", ""),
                "nickname": gi.get("owner_nickname", ""),
            },
            "note": 'The group is called "派 (Pai)" in the app.',
        }
    except Exception as exc:
        logger.exception("[yuanbao_tools] get_group_info error")
        return {"success": False, "error": str(exc)}


async def query_group_members(
    group_code: str,
    action: str = "list_all",
    name: str = "",
    mention: bool = False,
) -> dict:
    """
    统一的群成员查询工具（对齐 TS query_session_members）。

    action:
      - find      : 按昵称模糊搜索
      - list_bots : 列出 bot 和元宝 AI
      - list_all  : 列出全部成员
    """
    if not group_code:
        return {"success": False, "error": "group_code is required"}

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    try:
        raw = await adapter.get_group_member_list(group_code)
        if raw is None:
            return {"success": False, "error": "get_group_member_list returned None"}

        all_members = [
            {
                "user_id": m.get("user_id", ""),
                "nickname": m.get("nickname", m.get("nick_name", "")),
                "role": _USER_TYPE_LABEL.get(
                    m.get("user_type", m.get("role", 0)), "unknown"
                ),
            }
            for m in raw.get("members", [])
        ]

        if not all_members:
            return {"success": False, "error": "No members found in this group."}

        hint = {"mention_hint": MENTION_HINT} if mention else {}

        if action == "list_bots":
            bots = [m for m in all_members if m["role"] in ("yuanbao_ai", "bot")]
            if not bots:
                return {"success": False, "error": "No bots found in this group."}
            return {
                "success": True,
                "msg": f"Found {len(bots)} bot(s).",
                "members": bots,
                **hint,
            }

        if action == "find":
            if name:
                filt = name.strip().lower()
                matched = [m for m in all_members if filt in m["nickname"].lower()]
                if matched:
                    return {
                        "success": True,
                        "msg": f'Found {len(matched)} member(s) matching "{name}".',
                        "members": matched,
                        **hint,
                    }
                return {
                    "success": False,
                    "msg": f'No match for "{name}". All members listed below.',
                    "members": all_members,
                    **hint,
                }
            return {
                "success": True,
                "msg": f"Found {len(all_members)} member(s).",
                "members": all_members,
                **hint,
            }

        # list_all (default)
        return {
            "success": True,
            "msg": f"Found {len(all_members)} member(s).",
            "members": all_members,
            **hint,
        }

    except Exception as exc:
        logger.exception("[yuanbao_tools] query_group_members error")
        return {"success": False, "error": str(exc)}


async def search_sticker(query: str = "", limit: int = 10) -> dict:
    """
    在内置贴纸表中按关键词模糊搜索，返回 Top-N 候选。

    返回每条候选的 sticker_id / name / description / package_id，
    供 LLM 选择后传给 send_sticker。空 query 时返回前 N 条。
    """
    from gateway.platforms.yuanbao_sticker import search_stickers

    try:
        safe_limit = max(1, min(50, int(limit) if limit else 10))
    except (TypeError, ValueError):
        safe_limit = 10

    try:
        matches = search_stickers(query or "", limit=safe_limit)
    except Exception as exc:
        logger.exception("[yuanbao_tools] search_sticker error")
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "query": query or "",
        "count": len(matches),
        "results": [
            {
                "sticker_id": s.get("sticker_id", ""),
                "name": s.get("name", ""),
                "description": s.get("description", ""),
                "package_id": s.get("package_id", ""),
            }
            for s in matches
        ],
    }


async def send_sticker(
    sticker: str = "",
    chat_id: str = "",
    reply_to: str = "",
) -> dict:
    """
    向 chat_id（缺省取当前会话）发送一张内置贴纸（TIMFaceElem）。

    Args:
        sticker:   贴纸名称（如 "六六六"）或 sticker_id（如 "278"）。为空时随机发送一张。
        chat_id:   目标会话；缺省时使用当前会话上下文（HERMES_SESSION_CHAT_ID）。
                   格式：``direct:{account_id}`` / ``group:{group_code}`` / 或裸 account_id。
        reply_to:  群聊场景的引用消息 ID（可选）。

    Returns: ``{"success": bool, ...}``
    """
    from gateway.session_context import get_session_env
    from gateway.platforms.yuanbao_sticker import (
        get_sticker_by_id,
        get_sticker_by_name,
        get_random_sticker,
    )

    target = (chat_id or "").strip() or get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not target:
        return {
            "success": False,
            "error": "chat_id is required (no active yuanbao session detected)",
        }

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    raw = (sticker or "").strip()
    sticker_obj: Optional[dict] = None
    if not raw:
        sticker_obj = get_random_sticker()
    else:
        if raw.isdigit():
            sticker_obj = get_sticker_by_id(raw)
        if sticker_obj is None:
            sticker_obj = get_sticker_by_name(raw)

    if sticker_obj is None:
        return {
            "success": False,
            "error": f"Sticker not found: {raw!r}. "
                     f"Use search_sticker first to discover available stickers.",
        }

    try:
        result = await adapter.send_sticker(
            chat_id=target,
            sticker_name=sticker_obj.get("name", ""),
            reply_to=reply_to or None,
        )
    except Exception as exc:
        logger.exception("[yuanbao_tools] send_sticker error")
        return {"success": False, "error": str(exc)}

    if getattr(result, "success", False):
        return {
            "success": True,
            "chat_id": target,
            "sticker": {
                "sticker_id": sticker_obj.get("sticker_id", ""),
                "name": sticker_obj.get("name", ""),
            },
            "message_id": getattr(result, "message_id", None),
        }
    return {
        "success": False,
        "error": getattr(result, "error", "send_sticker failed"),
    }


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_result, tool_error  # noqa: E402


def _check_yuanbao():
    """Toolset availability check — True when running in a yuanbao gateway session."""
    try:
        from gateway.session_context import get_session_env
        if get_session_env("HERMES_SESSION_PLATFORM", "") == "yuanbao":
            return True
    except Exception:
        pass
    return _get_active_adapter() is not None


async def _handle_yb_query_group_info(args, **kw):
    return tool_result(await get_group_info(
        group_code=args.get("group_code", ""),
    ))


async def _handle_yb_query_group_members(args, **kw):
    return tool_result(await query_group_members(
        group_code=args.get("group_code", ""),
        action=args.get("action", "list_all"),
        name=args.get("name", ""),
        mention=bool(args.get("mention", False)),
    ))


async def _handle_yb_search_sticker(args, **kw):
    return tool_result(await search_sticker(
        query=args.get("query", ""),
        limit=args.get("limit", 10),
    ))


async def _handle_yb_send_sticker(args, **kw):
    return tool_result(await send_sticker(
        sticker=args.get("sticker", ""),
        chat_id=args.get("chat_id", ""),
        reply_to=args.get("reply_to", ""),
    ))


_TOOLSET = "hermes-yuanbao"

registry.register(
    name="yb_query_group_info",
    toolset=_TOOLSET,
    schema={
        "name": "yb_query_group_info",
        "description": (
            "Query basic info about a group (called '派/Pai' in the app), "
            "including group name, owner, and member count."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "The unique group identifier (group_code).",
                },
            },
            "required": ["group_code"],
        },
    },
    handler=_handle_yb_query_group_info,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="👥",
)

registry.register(
    name="yb_query_group_members",
    toolset=_TOOLSET,
    schema={
        "name": "yb_query_group_members",
        "description": (
            "Query members of a group (called '派/Pai' in the app). "
            "Use this tool when you need to @mention someone, find a user by name, "
            "list bots (including Yuanbao AI), or list all members. "
            "IMPORTANT: You MUST call this tool before @mentioning any user, "
            "because you need the exact nickname to construct the @mention format."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "The unique group identifier (group_code).",
                },
                "action": {
                    "type": "string",
                    "enum": ["find", "list_bots", "list_all"],
                    "description": (
                        "find — search a user by name (use when you need to @mention or look up someone); "
                        "list_bots — list bots and Yuanbao AI assistants; "
                        "list_all — list all members."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "User name to search (partial match, case-insensitive). "
                        "Required for 'find'. Use the name the user mentioned in the conversation."
                    ),
                },
                "mention": {
                    "type": "boolean",
                    "description": (
                        "Set to true when you need to @mention/at someone in your reply. "
                        "The response will include the exact @mention format to use."
                    ),
                },
            },
            "required": ["group_code", "action"],
        },
    },
    handler=_handle_yb_query_group_members,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="📋",
)


registry.register(
    name="yb_search_sticker",
    toolset=_TOOLSET,
    schema={
        "name": "yb_search_sticker",
        "description": (
            "Search the built-in Yuanbao sticker (TIM face / 表情包) catalogue by keyword. "
            "Returns the top matching candidates with sticker_id, name, and description. "
            "Use this BEFORE yb_send_sticker to discover the right sticker_id. "
            "Sticker = 贴纸 = TIM face — NOT a message reaction. "
            "Prefer sending a sticker over bare Unicode emoji when reacting/expressing emotion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search keyword (Chinese or English, e.g. '666', '比心', 'cool', '吃瓜'). "
                        "Empty string returns the first N stickers."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of candidates to return (default 10, max 50).",
                },
            },
            "required": [],
        },
    },
    handler=_handle_yb_search_sticker,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="🔍",
)


registry.register(
    name="yb_send_sticker",
    toolset=_TOOLSET,
    schema={
        "name": "yb_send_sticker",
        "description": (
            "Send a built-in sticker (TIMFaceElem / 贴纸表情) to the current Yuanbao chat. "
            "Call yb_search_sticker first if you don't know the sticker_id/name. "
            "Sticker = 贴纸 = TIM face — NOT a message reaction. "
            "CRITICAL: Whenever the user asks you to send a sticker / 贴纸 / 表情包, you MUST "
            "use this tool. DO NOT draw a PNG via execute_code / Pillow / matplotlib and "
            "then call send_image_file — that produces a fake 'sticker' image instead of a "
            "real TIM face and is the WRONG path. If no suitable sticker_id is known, call "
            "yb_search_sticker first. When the recent thread shows users sending stickers, "
            "prefer matching that tone by replying with a sticker instead of (or in "
            "addition to) text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sticker": {
                    "type": "string",
                    "description": (
                        "Sticker name (e.g. '六六六', '比心', 'ok') or numeric sticker_id "
                        "(e.g. '278'). Empty string sends a random built-in sticker."
                    ),
                },
                "chat_id": {
                    "type": "string",
                    "description": (
                        "Target chat. Defaults to the current session. "
                        "Format: 'direct:{account_id}', 'group:{group_code}', or bare account_id."
                    ),
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional ref_msg_id to quote-reply (group chat only).",
                },
            },
            "required": [],
        },
    },
    handler=_handle_yb_send_sticker,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="🎨",
)

