"""Tests for tools/napcat_tool.py — the generic OneBot 11 action proxy."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def napcat_module():
    """Import the napcat_tool module fresh on demand."""
    # Lazily ensure aiohttp shim so napcat platform module imports cleanly.
    sys.modules.pop("tools.napcat_tool", None)
    import tools.napcat_tool as mod  # noqa: WPS433 — intentional reimport
    return mod


def _stub_runner(adapter):
    """Build a fake gateway runner with one NapCat adapter slot."""
    from gateway.config import Platform

    return SimpleNamespace(adapters={Platform.NAPCAT: adapter})


@contextmanager
def _patch_active_runner(runner):
    """Context manager that exposes ``gateway.run._active_runner`` for the tool.

    ``tools.napcat_tool`` resolves the runner with ``import gateway.run as
    _run`` at call-time. Importing the real :mod:`gateway.run` is heavy
    (it pulls in optional deps like ``dotenv``), so we install a tiny stub
    module under ``sys.modules['gateway.run']`` for the duration of the
    test and restore the previous mapping on exit.
    """
    import types

    sentinel = object()
    previous = sys.modules.get("gateway.run", sentinel)
    stub = types.ModuleType("gateway.run")
    stub._active_runner = runner
    sys.modules["gateway.run"] = stub

    # Make sure ``import gateway`` exposes ``run`` so attribute access works
    # for code that does ``gateway.run._active_runner`` rather than the
    # ``import gateway.run`` form.
    import gateway  # noqa: WPS433

    prev_run_attr = getattr(gateway, "run", sentinel)
    setattr(gateway, "run", stub)
    try:
        yield
    finally:
        if previous is sentinel:
            sys.modules.pop("gateway.run", None)
        else:
            sys.modules["gateway.run"] = previous
        if prev_run_attr is sentinel:
            try:
                delattr(gateway, "run")
            except AttributeError:
                pass
        else:
            setattr(gateway, "run", prev_run_attr)


# ---------------------------------------------------------------------------
# check_napcat_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_session_platform_napcat_returns_true(self, napcat_module, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "napcat")
        # Even with no live runner, the session-platform fast path wins.
        with _patch_active_runner(None):
            assert napcat_module.check_napcat_available() is True

    def test_no_runner_returns_false(self, napcat_module, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
        with _patch_active_runner(None):
            assert napcat_module.check_napcat_available() is False

    def test_runner_with_disconnected_adapter_returns_false(self, napcat_module, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
        adapter = SimpleNamespace(is_connected=False)
        with _patch_active_runner(_stub_runner(adapter)):
            assert napcat_module.check_napcat_available() is False

    def test_runner_with_connected_adapter_returns_true(self, napcat_module, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
        adapter = SimpleNamespace(is_connected=True)
        with _patch_active_runner(_stub_runner(adapter)):
            assert napcat_module.check_napcat_available() is True


# ---------------------------------------------------------------------------
# napcat_call dispatch
# ---------------------------------------------------------------------------


class TestNapcatCallDispatch:
    def test_missing_action_returns_error(self, napcat_module):
        result = json.loads(napcat_module.napcat_call(action=""))
        assert "error" in result

    def test_invalid_params_type_returns_error(self, napcat_module):
        result = json.loads(napcat_module.napcat_call(action="get_status", params="not-a-dict"))
        assert "error" in result

    def test_no_runner_returns_error(self, napcat_module):
        with _patch_active_runner(None):
            payload = json.loads(napcat_module.napcat_call(action="get_status", params={}))
        assert "error" in payload
        assert "gateway" in payload["error"].lower()

    def test_disconnected_adapter_returns_error(self, napcat_module):
        adapter = SimpleNamespace(
            is_connected=False,
            call_action=AsyncMock(),
        )
        with _patch_active_runner(_stub_runner(adapter)):
            payload = json.loads(napcat_module.napcat_call(action="get_status", params={}))
        assert "error" in payload
        adapter.call_action.assert_not_awaited()

    def test_successful_call_routes_action_and_params(self, napcat_module):
        ok_response = {
            "status": "ok",
            "retcode": 0,
            "data": {"messages": [{"message_id": "abc"}]},
            "echo": "xyz",
        }
        adapter = SimpleNamespace(
            is_connected=True,
            call_action=AsyncMock(return_value=ok_response),
        )
        with _patch_active_runner(_stub_runner(adapter)):
            raw = napcat_module.napcat_call(
                action="get_group_msg_history",
                params={"group_id": 12345, "count": 10},
            )
        payload = json.loads(raw)
        assert payload["success"] is True
        assert payload["action"] == "get_group_msg_history"
        assert payload["retcode"] == 0
        assert payload["data"]["messages"][0]["message_id"] == "abc"
        adapter.call_action.assert_awaited_once_with(
            "get_group_msg_history",
            {"group_id": 12345, "count": 10},
        )

    def test_failed_call_surfaces_error_and_raw(self, napcat_module):
        bad_response = {
            "status": "failed",
            "retcode": 100,
            "message": "GROUP_NOT_FOUND",
            "wording": "群不存在",
        }
        adapter = SimpleNamespace(
            is_connected=True,
            call_action=AsyncMock(return_value=bad_response),
        )
        with _patch_active_runner(_stub_runner(adapter)):
            raw = napcat_module.napcat_call(action="get_group_info", params={"group_id": 1})
        payload = json.loads(raw)
        assert "error" in payload
        assert payload["retcode"] == 100
        assert payload["raw"]["wording"] == "群不存在"

    def test_runtime_error_in_call_action_returns_error(self, napcat_module):
        adapter = SimpleNamespace(
            is_connected=True,
            call_action=AsyncMock(side_effect=RuntimeError("websocket closed")),
        )
        with _patch_active_runner(_stub_runner(adapter)):
            raw = napcat_module.napcat_call(action="delete_msg", params={"message_id": "x"})
        payload = json.loads(raw)
        assert "error" in payload
        assert "websocket closed" in payload["error"]

    def test_default_params_are_empty_dict(self, napcat_module):
        ok = {"status": "ok", "retcode": 0, "data": {"online": True}}
        adapter = SimpleNamespace(
            is_connected=True,
            call_action=AsyncMock(return_value=ok),
        )
        with _patch_active_runner(_stub_runner(adapter)):
            raw = napcat_module.napcat_call(action="get_status")
        payload = json.loads(raw)
        assert payload["success"] is True
        adapter.call_action.assert_awaited_once_with("get_status", {})


# ---------------------------------------------------------------------------
# Registry integration — tool is registered correctly
# ---------------------------------------------------------------------------


class TestRegistryRegistration:
    def test_napcat_call_is_registered(self, napcat_module):
        from tools.registry import registry

        entry = registry.get_entry("napcat_call")
        assert entry is not None
        assert entry.toolset == "napcat"
        assert callable(entry.handler)
        assert entry.check_fn is napcat_module.check_napcat_available

    def test_handler_unwraps_args(self, napcat_module):
        """Registry handler shape: handler(args_dict, **kw) -> JSON string."""
        from tools.registry import registry

        entry = registry.get_entry("napcat_call")
        ok = {"status": "ok", "retcode": 0, "data": {"x": 1}}
        adapter = SimpleNamespace(
            is_connected=True,
            call_action=AsyncMock(return_value=ok),
        )
        with _patch_active_runner(_stub_runner(adapter)):
            raw = entry.handler(
                {"action": "get_status", "params": {"a": 1}},
                task_id="task-1",
            )
        payload = json.loads(raw)
        assert payload["success"] is True
        adapter.call_action.assert_awaited_once_with("get_status", {"a": 1})
