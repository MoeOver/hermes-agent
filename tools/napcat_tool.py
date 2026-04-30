"""NapCat OneBot 11 generic action tool.

Exposes a single ``napcat_call(action, params)`` tool that proxies any OneBot 11
action through the running NapCat gateway adapter.

Architecture
------------
NapCat connects to Hermes via reverse WebSocket; only the ``GatewayRunner``
process holds the live connection. This tool reaches into the running runner
(``gateway.run._active_runner``), grabs the NapCat adapter, and forwards the
action via the adapter's public ``call_action`` method.

Availability is gated by ``check_napcat_available()`` so the tool only appears
when:

- the current session declares ``HERMES_SESSION_PLATFORM=napcat``, OR
- the in-process gateway runner has a connected NapCat adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Availability gating
# ----------------------------------------------------------------------

def _get_napcat_adapter():
    """Return the running NapCat adapter, or None when unavailable."""
    try:
        from gateway.config import Platform
        import gateway.run as _run
    except Exception:
        return None
    runner = getattr(_run, "_active_runner", None)
    if runner is None:
        return None
    try:
        return runner.adapters.get(Platform.NAPCAT)
    except Exception:
        return None


def check_napcat_available() -> bool:
    """Gate ``napcat_call`` on session platform == napcat OR live NapCat adapter."""
    try:
        from gateway.session_context import get_session_env
        if get_session_env("HERMES_SESSION_PLATFORM", "") == "napcat":
            return True
    except Exception:
        pass
    adapter = _get_napcat_adapter()
    return adapter is not None and bool(getattr(adapter, "is_connected", False))


# ----------------------------------------------------------------------
# Action dispatch
# ----------------------------------------------------------------------

async def _do_napcat_call(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Forward an OneBot 11 action through the running NapCat adapter."""
    adapter = _get_napcat_adapter()
    if adapter is None:
        return {
            "error": (
                "NapCat call requires the gateway to be running with the NapCat "
                "adapter loaded. Start it via `hermes gateway run` and ensure "
                "NapCat is configured."
            )
        }
    if not getattr(adapter, "is_connected", False):
        return {
            "error": (
                "NapCat adapter exists but is not connected — open the NapCat "
                "client and let it connect to the reverse WebSocket first."
            )
        }
    call_action = getattr(adapter, "call_action", None)
    if not callable(call_action):
        return {"error": "NapCat adapter does not expose call_action(); upgrade the gateway."}

    try:
        response = await call_action(action, params or {})
    except asyncio.TimeoutError:
        return {"error": f"NapCat action '{action}' timed out waiting for response."}
    except RuntimeError as exc:
        return {"error": f"NapCat action '{action}' failed: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"NapCat action '{action}' raised: {exc}"}

    if not isinstance(response, dict):
        return {"error": f"NapCat returned non-dict response: {type(response).__name__}"}

    status = response.get("status")
    retcode = response.get("retcode", 0)
    if status != "ok" or retcode != 0:
        msg = response.get("message") or response.get("wording") or "OneBot action failed"
        return {
            "error": f"NapCat {action} failed (retcode={retcode}): {msg}",
            "retcode": retcode,
            "status": status,
            "raw": response,
        }

    return {
        "success": True,
        "action": action,
        "status": status,
        "retcode": retcode,
        "data": response.get("data"),
    }


def napcat_call(action: str, params: Optional[Dict[str, Any]] = None,
                task_id: Optional[str] = None) -> str:
    """Tool entrypoint — JSON in, JSON out."""
    del task_id
    if not action or not isinstance(action, str):
        return tool_error("'action' is required and must be an OneBot 11 endpoint name (string).")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return tool_error("'params' must be an object/dict of action parameters.")

    try:
        from model_tools import _run_async
        result = _run_async(_do_napcat_call(action, params))
    except Exception as exc:
        return json.dumps({"error": f"napcat_call dispatch failed: {exc}"}, ensure_ascii=False)

    return json.dumps(result, ensure_ascii=False)


# ----------------------------------------------------------------------
# Schema (description embeds a compact OneBot 11 action catalog)
# ----------------------------------------------------------------------

NAPCAT_CALL_SCHEMA = {
    "name": "napcat_call",
    "description": (
        "Invoke any OneBot 11 / NapCat action against the connected QQ account. "
        "Use this for QQ operations beyond plain text replies — fetch group history, "
        "send images/voice/files, manage group members, modify your QQ profile, "
        "recall messages, react with emojis, and so on.\n\n"
        "IMPORTANT — output style for QQ replies (the underlying adapter is NapCat): "
        "QQ does NOT render Markdown. When composing user-visible replies use plain "
        "text only — no **bold**, no #, no -, no `code`, no fenced blocks, no tables. "
        "Keep replies short.\n\n"
        "Common action names (pass as `action`, with appropriate `params`):\n"
        "  Messages: send_msg, send_private_msg, send_group_msg, delete_msg, get_msg, "
        "get_forward_msg, get_group_msg_history, send_group_forward_msg, "
        "send_private_forward_msg, mark_msg_as_read, set_msg_emoji_like.\n"
        "  Group mgmt: get_group_info, get_group_list, get_group_member_info, "
        "get_group_member_list, set_group_card, set_group_kick, set_group_ban, "
        "set_group_whole_ban, set_group_admin, set_group_name, set_group_leave, "
        "set_group_special_title, set_group_add_request.\n"
        "  Friends: get_friend_list, send_like, set_friend_add_request, delete_friend.\n"
        "  Files/media: get_image, get_record, get_file, download_file, "
        "upload_group_file, upload_private_file, can_send_image, can_send_record. "
        "Prefer the MEDIA: tag for outbound images (gateway routes to send_image_file).\n"
        "  Profile: set_qq_profile, get_login_info, get_stranger_info.\n"
        "  System: get_status, get_version_info, clean_cache, ocr_image.\n\n"
        "Returns JSON: on success {success:true, action, retcode:0, data:{...}}; on "
        "failure {error, retcode, raw}. Refer to the `napcat` skill for parameter shapes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "OneBot 11 action endpoint name, e.g. 'get_group_msg_history', "
                    "'set_qq_profile', 'delete_msg', 'send_like'."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Action parameters as a JSON object. Shape depends on the action. "
                    "Examples: get_group_msg_history → {group_id:123, count:20}; "
                    "set_qq_profile → {nickname:'...'}; delete_msg → {message_id:'...'}; "
                    "send_like → {user_id:10001, times:1}; "
                    "get_group_member_info → {group_id:123, user_id:10001}."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="napcat_call",
    toolset="napcat",
    schema=NAPCAT_CALL_SCHEMA,
    handler=lambda args, **kw: napcat_call(
        action=args.get("action", ""),
        params=args.get("params") or {},
        task_id=kw.get("task_id"),
    ),
    check_fn=check_napcat_available,
    emoji="🐱",
)
