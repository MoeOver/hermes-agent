"""
NapCat platform adapter speaking OneBot 11 over reverse WebSocket.

Architecture
------------
Hermes runs a lightweight ``aiohttp`` HTTP server and exposes a single
WebSocket endpoint (``/napcat/ws`` by default).  NapCat is configured to
connect *to* Hermes as a ``websocketClients`` entry — this is the pattern
that best matches the common deployment topology where NapCat runs on a
Windows/desktop QQ machine and Hermes runs on a separate Linux/VPS host.

Responsibilities of this adapter:

- Authenticate NapCat with a shared token (``Authorization`` header or
  ``access_token`` query parameter).
- Receive OneBot 11 events (``meta_event``, ``message``) and translate
  them into a Hermes :class:`MessageEvent`.
- Send replies via OneBot actions (``send_private_msg`` /
  ``send_group_msg``) over the same WebSocket, using the ``echo`` field
  for request/response correlation.
- Track the active connection's ``self_id`` so group mention gating can
  tell whether the bot itself was addressed.

The adapter intentionally supports **one** active NapCat connection at a
time — every inbound connection replaces the previous one.  This mirrors
how NapCat's reverse WebSocket client works: a NapCat instance owns a
single QQ account, and that account is expected to connect to a single
gateway endpoint.

Reference: https://mintlify.wiki/NapNeko/NapCatQQ/api/onebot/overview
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    from aiohttp import WSCloseCode, WSMsgType, web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive import guard
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]
    WSCloseCode = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8646
DEFAULT_PATH = "/napcat/ws"
DEFAULT_SEND_TIMEOUT = 20.0
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 2000
MAX_MESSAGE_LENGTH = 4500  # OneBot has no strict cap; keep generous & chunk safely


def check_napcat_requirements() -> bool:
    """Return True if the optional runtime dependencies are present."""
    return AIOHTTP_AVAILABLE


class NapCatAdapter(BasePlatformAdapter):
    """Reverse WebSocket adapter for NapCatQQ / OneBot 11."""

    # OneBot doesn't support editing sent messages.
    SUPPORTS_MESSAGE_EDITING = False

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.NAPCAT)

        extra = config.extra or {}
        self._token: str = str(
            extra.get("token") or config.token or os.getenv("NAPCAT_TOKEN", "")
        ).strip()
        self._host: str = str(extra.get("host") or os.getenv("NAPCAT_HOST", DEFAULT_HOST))
        try:
            self._port: int = int(extra.get("port") or os.getenv("NAPCAT_PORT", DEFAULT_PORT))
        except (TypeError, ValueError):
            self._port = DEFAULT_PORT
        self._path: str = str(extra.get("path") or os.getenv("NAPCAT_PATH", DEFAULT_PATH))
        if not self._path.startswith("/"):
            self._path = "/" + self._path

        # Runtime state
        self._runner: Optional["web.AppRunner"] = None  # type: ignore[name-defined]
        self._site: Optional["web.TCPSite"] = None  # type: ignore[name-defined]
        self._ws: Optional["web.WebSocketResponse"] = None  # type: ignore[name-defined]
        self._ws_lock = asyncio.Lock()
        self._self_id: Optional[str] = None
        self._chat_type_map: Dict[str, str] = {}
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._seen_messages: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "NapCat"

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error(
                "napcat_missing_dependency",
                "aiohttp is required for the NapCat adapter",
                retryable=True,
            )
            return False

        if not self._token:
            self._set_fatal_error(
                "napcat_missing_token",
                "NAPCAT_TOKEN is required — set a shared secret to authenticate NapCat",
                retryable=True,
            )
            return False

        if not self._acquire_platform_lock(
            "napcat-bind", f"{self._host}:{self._port}{self._path}", "NapCat bind address"
        ):
            return False

        try:
            app = web.Application()
            app.router.add_get("/health", self._handle_health)
            app.router.add_get(self._path, self._handle_ws)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._mark_connected()
            logger.info(
                "[%s] Reverse WebSocket listening on ws://%s:%d%s",
                self.name, self._host, self._port, self._path,
            )
            return True
        except Exception as exc:
            self._set_fatal_error(
                "napcat_bind_error", f"NapCat startup failed: {exc}", retryable=True,
            )
            logger.error("[%s] startup failed: %s", self.name, exc, exc_info=True)
            await self._teardown_server()
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

        if self._ws is not None:
            try:
                await self._ws.close(code=WSCloseCode.GOING_AWAY, message=b"gateway shutdown")
            except Exception:
                pass
            self._ws = None

        await self._teardown_server()
        self._fail_pending("Disconnected")
        self._release_platform_lock()
        logger.info("[%s] Disconnected", self.name)

    async def _teardown_server(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending_responses.clear()

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request):  # pragma: no cover - trivial
        return web.json_response({"status": "ok", "platform": self.platform.value})

    async def _handle_ws(self, request):
        if not self._is_authorized_request(request):
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=0)
        await ws.prepare(request)

        # NapCat sends X-Self-ID with the upgrade request.
        self_id_header = request.headers.get("X-Self-ID")
        if self_id_header:
            self._self_id = str(self_id_header).strip()

        # Replace any existing connection — NapCat only serves one QQ account.
        previous = self._ws
        self._ws = ws
        if previous is not None and not previous.closed:
            try:
                await previous.close(code=WSCloseCode.GOING_AWAY, message=b"superseded")
            except Exception:
                pass

        logger.info(
            "[%s] NapCat connected (self_id=%s, remote=%s)",
            self.name, self._self_id, request.remote,
        )

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    payload = self._safe_parse_json(msg.data)
                    if payload is not None:
                        await self._handle_ws_payload(payload)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning(
                        "[%s] WebSocket error: %s", self.name, ws.exception(),
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] websocket handler failed", self.name)
        finally:
            if self._ws is ws:
                self._ws = None
            self._fail_pending("Connection closed")
            logger.info("[%s] NapCat disconnected", self.name)
        return ws

    def _is_authorized_request(self, request) -> bool:
        """Check Authorization header / access_token query against configured token."""
        if not self._token:
            return False
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            if header[7:].strip() == self._token:
                return True
        elif header.strip() == self._token:
            return True
        query_token = request.rel_url.query.get("access_token") or request.query.get("access_token")
        if query_token and str(query_token).strip() == self._token:
            return True
        return False

    @staticmethod
    def _safe_parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Inbound event handling
    # ------------------------------------------------------------------

    async def _handle_ws_payload(self, payload: Dict[str, Any]) -> None:
        # API responses (no post_type, carries echo)
        echo = payload.get("echo")
        if echo and "post_type" not in payload:
            fut = self._pending_responses.pop(str(echo), None)
            if fut and not fut.done():
                fut.set_result(payload)
            return

        post_type = payload.get("post_type")
        if post_type == "meta_event":
            self._handle_meta_event(payload)
            return
        if post_type == "message":
            event = self._build_message_event(payload)
            if event is not None:
                self._dispatch_message_event(event)
            return
        # notice / request events are not surfaced to the agent yet.
        logger.debug("[%s] unhandled post_type=%s", self.name, post_type)

    def _dispatch_message_event(self, event: MessageEvent) -> None:
        """Process inbound messages without blocking the websocket read loop."""
        task = asyncio.create_task(self.handle_message(event))
        try:
            self._background_tasks.add(task)
        except TypeError:
            return
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._expected_cancelled_tasks.discard)

    def _handle_meta_event(self, payload: Dict[str, Any]) -> None:
        self_id = payload.get("self_id")
        if self_id is not None:
            self._self_id = str(self_id)
        meta_type = payload.get("meta_event_type")
        if meta_type == "lifecycle":
            logger.info(
                "[%s] lifecycle=%s self_id=%s",
                self.name, payload.get("sub_type"), self._self_id,
            )

    def _build_message_event(self, payload: Dict[str, Any]) -> Optional[MessageEvent]:
        message_id = payload.get("message_id")
        if message_id is None:
            return None
        msg_key = str(message_id)
        if self._is_duplicate(msg_key):
            return None

        message_type = payload.get("message_type")
        segments = self._normalize_segments(payload.get("message"))
        reply_to, text = self._extract_reply_and_text(segments)

        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        sender_id = str(sender.get("user_id") or payload.get("user_id") or "")
        sender_name = (
            str(sender.get("card") or "").strip()
            or str(sender.get("nickname") or "").strip()
            or None
        )

        if message_type == "private":
            chat_id = sender_id or str(payload.get("user_id") or "")
            if not chat_id:
                return None
            if not text:
                return None
            self._chat_type_map[chat_id] = "private"
            source = self.build_source(
                chat_id=chat_id,
                user_id=sender_id or chat_id,
                user_name=sender_name,
                chat_type="dm",
            )
            return MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=msg_key,
                reply_to_message_id=reply_to,
            )

        if message_type == "group":
            group_id = str(payload.get("group_id") or "")
            if not group_id:
                return None

            # Group mention gating: require an explicit @bot segment.
            mentioned, stripped_text = self._strip_self_mention(segments)
            if not mentioned:
                return None
            if not stripped_text:
                return None

            self._chat_type_map[group_id] = "group"
            source = self.build_source(
                chat_id=group_id,
                user_id=sender_id or None,
                user_name=sender_name,
                chat_type="group",
            )
            return MessageEvent(
                text=stripped_text,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=msg_key,
                reply_to_message_id=reply_to,
            )

        return None

    @staticmethod
    def _normalize_segments(raw_message: Any) -> List[Dict[str, Any]]:
        """Return OneBot 11 array-format segments from any inbound shape."""
        if isinstance(raw_message, list):
            return [seg for seg in raw_message if isinstance(seg, dict)]
        if isinstance(raw_message, dict):
            return [raw_message]
        if isinstance(raw_message, str):
            # String-format (CQ code) — treat the whole string as plain text.
            # CQ code parsing is intentionally out of scope; recommend
            # ``messagePostFormat: array`` in documentation.
            stripped = re.sub(r"\[CQ:[^\]]*\]", "", raw_message)
            if stripped.strip():
                return [{"type": "text", "data": {"text": stripped}}]
        return []

    @staticmethod
    def _extract_reply_and_text(segments: List[Dict[str, Any]]):
        reply_to: Optional[str] = None
        text_parts: List[str] = []
        for seg in segments:
            seg_type = seg.get("type")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg_type == "reply":
                value = data.get("id")
                if value is not None and reply_to is None:
                    reply_to = str(value)
            elif seg_type == "text":
                value = data.get("text")
                if isinstance(value, str):
                    text_parts.append(value)
        return reply_to, " ".join(part.strip() for part in text_parts if part.strip()).strip()

    def _strip_self_mention(self, segments: List[Dict[str, Any]]):
        """Return (mentioned, cleaned_text) with the @self_id segment removed."""
        if not self._self_id:
            return False, ""
        mentioned = False
        text_parts: List[str] = []
        reply_seen = False
        for seg in segments:
            seg_type = seg.get("type")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg_type == "at":
                qq = str(data.get("qq") or "").strip()
                if qq == self._self_id:
                    mentioned = True
                continue
            if seg_type == "reply":
                # Reply to a message authored by the bot counts as a mention —
                # this lets users keep a thread going without re-@'ing the bot.
                target = str(data.get("id") or "")
                if target:
                    reply_seen = True
                continue
            if seg_type == "text":
                value = data.get("text")
                if isinstance(value, str):
                    text_parts.append(value)
        cleaned = " ".join(part.strip() for part in text_parts if part.strip()).strip()
        return mentioned or reply_seen, cleaned

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {
                key: ts for key, ts in self._seen_messages.items() if ts > cutoff
            }
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        if not self.is_connected:
            return SendResult(success=False, error="Not connected")
        if self._ws is None or getattr(self._ws, "closed", True):
            return SendResult(success=False, error="NapCat is not connected", retryable=True)
        if not content or not content.strip():
            return SendResult(success=True)

        chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH)
        last_result = SendResult(success=False, error="No chunks")
        for idx, chunk in enumerate(chunks):
            last_result = await self._send_chunk(
                chat_id, chunk, reply_to=reply_to if idx == 0 else None,
            )
            if not last_result.success:
                return last_result
        return last_result

    async def _send_chunk(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str],
    ) -> SendResult:
        chat_type = self._chat_type_map.get(chat_id)
        normalized_id = chat_id
        if not chat_type:
            if chat_id.startswith("group:"):
                chat_type = "group"
                normalized_id = chat_id.split(":", 1)[1]
            elif chat_id.startswith("private:"):
                chat_type = "private"
                normalized_id = chat_id.split(":", 1)[1]
            else:
                # Heuristic: a plain numeric ID is assumed to be a private chat
                # (QQ numbers).  Group IDs are cached when the adapter receives
                # their first inbound message.
                chat_type = "private"

        message_segments: List[Dict[str, Any]] = []
        if reply_to:
            message_segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        message_segments.append({"type": "text", "data": {"text": content}})

        if chat_type == "group":
            action = "send_group_msg"
            params: Dict[str, Any] = {
                "group_id": self._coerce_int(normalized_id),
                "message": message_segments,
            }
        else:
            action = "send_private_msg"
            params = {
                "user_id": self._coerce_int(normalized_id),
                "message": message_segments,
            }

        try:
            response = await self._call_action(action, params)
        except asyncio.TimeoutError:
            # Waiting for the OneBot echo timed out after we already wrote the
            # request to the socket. The message may have been delivered, so do
            # not auto-retry and risk duplicate sends.
            return SendResult(success=False, error="NapCat send timed out")
        except RuntimeError as exc:
            error = str(exc)
            return SendResult(
                success=False,
                error=error,
                retryable=self._is_retryable_runtime_send_error(error),
            )

        if response.get("status") != "ok" or response.get("retcode", 0) != 0:
            return SendResult(
                success=False,
                error=response.get("message") or response.get("wording") or "send failed",
                raw_response=response,
            )

        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        message_id = data.get("message_id")
        return SendResult(
            success=True,
            message_id=str(message_id) if message_id is not None else None,
            raw_response=response,
        )

    async def _call_action(
        self,
        action: str,
        params: Dict[str, Any],
        *,
        timeout: float = DEFAULT_SEND_TIMEOUT,
    ) -> Dict[str, Any]:
        if self._ws is None or getattr(self._ws, "closed", True):
            raise RuntimeError("NapCat websocket not connected")
        echo = uuid.uuid4().hex
        payload = {"action": action, "params": params, "echo": echo}
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_responses[echo] = future
        async with self._ws_lock:
            try:
                await self._ws.send_json(payload)
            except Exception as exc:
                self._pending_responses.pop(echo, None)
                if not future.done():
                    future.set_exception(exc)
                raise
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_responses.pop(echo, None)
            raise

    @staticmethod
    def _coerce_int(value: Any) -> Any:
        """Return ``value`` as int when possible, otherwise pass through."""
        if isinstance(value, bool):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _is_retryable_runtime_send_error(error: str) -> bool:
        """Return True only for failures that happen before sending anything."""
        lowered = error.lower()
        return "websocket not connected" in lowered

    # ------------------------------------------------------------------
    # BasePlatformAdapter hooks
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = self._chat_type_map.get(chat_id, "private")
        return {
            "name": chat_id,
            "type": "group" if chat_type == "group" else "dm",
        }
