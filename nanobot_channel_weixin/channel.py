"""Personal WeChat channel for nanobot via iLink Bot long-poll API."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

from nanobot_channel_weixin.api import (
    CDN_BASE_URL,
    DEFAULT_BASE_URL,
    TYPING_STATUS_CANCEL,
    TYPING_STATUS_TYPING,
    download_cdn_media,
    get_config,
    get_updates,
    send_media_message,
    send_message,
    send_typing,
    upload_cdn_file,
)
from nanobot_channel_weixin.auth import (
    AccountData,
    get_default_account,
    load_sync_buf,
    save_sync_buf,
)

class _DictConfig:
    """Thin wrapper so BaseChannel.is_allowed() can read allow_from via getattr."""

    def __init__(self, d: dict):
        self._d = d
        self.allow_from = d.get("allowFrom", d.get("allow_from", []))

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]

    def __contains__(self, key):
        return key in self._d


_SESSION_EXPIRED = -14
_MAX_FAILURES = 3
_BACKOFF_S = 30
_RETRY_S = 2
_SESSION_PAUSE_S = 300
_TYPING_KEEPALIVE_S = 5
_CONFIG_CACHE_TTL_S = 24 * 3600


def _strip_markdown(text: str) -> str:
    """Lightweight markdown → plain text for WeChat delivery."""
    s = text
    s = re.sub(r"```[^\n]*\n?([\s\S]*?)```", lambda m: m.group(1).strip(), s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\|[\s:|\-]+\|$", "", s, flags=re.MULTILINE)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)
    return s


def _media_dir() -> str:
    """Resolve the weixin media directory.

    Uses nanobot's configured media path (~/.nanobot/media/weixin/) when running
    inside the gateway. Falls back to /tmp/nanobot/media/weixin/ when nanobot
    config is unavailable (e.g. standalone CLI testing).
    """
    try:
        from nanobot.config.paths import get_media_dir
        return str(get_media_dir("weixin"))
    except Exception:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "nanobot", "media", "weixin")
        os.makedirs(d, exist_ok=True)
        return d


class WeixinChannel(BaseChannel):
    """
    Personal WeChat channel.

    Login:   nanobot-weixin login
    Config:  channels.weixin.enabled = true  in ~/.nanobot/config.json
    """

    name = "weixin"
    display_name = "WeChat"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {
            "enabled": False,
            "baseUrl": DEFAULT_BASE_URL,
            "cdnBaseUrl": CDN_BASE_URL,
            "allowFrom": ["*"],
        }

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            cfg = _DictConfig(config)
        else:
            cfg = config or _DictConfig({})
        super().__init__(cfg, bus)
        self._cfg = config if isinstance(config, dict) else {}
        self._context_tokens: dict[str, str] = {}
        self._account: AccountData | None = None
        # typing_ticket cache: {user_id: (ticket, fetched_at)}
        self._typing_tickets: dict[str, tuple[str, float]] = {}
        # active keepalive tasks: {user_id: asyncio.Task}
        self._typing_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

    async def start(self) -> None:
        """Start the long-poll monitor (blocks forever)."""
        self._account = get_default_account()
        if not self._account or not self._account.configured:
            logger.error(
                "WeChat: no configured account. Run: nanobot-weixin login"
            )
            return

        self._running = True
        logger.info(
            "WeChat channel starting: account={} base_url={}",
            self._account.account_id,
            self._account.base_url,
        )

        await self._poll_loop(self._account)

    async def stop(self) -> None:
        self._running = False
        logger.info("WeChat channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a reply back through WeChat (text and/or media)."""
        if not self._account or not self._account.configured:
            logger.warning("WeChat: cannot send, account not configured")
            return

        to = msg.chat_id

        if msg.metadata.get("_progress"):
            # Progress messages mean the agent is still working. Don't forward
            # them to WeChat, but (re)start typing if it was previously stopped.
            task = self._typing_tasks.get(to)
            if not task or task.done():
                ticket = self._typing_tickets.get(to, ("", 0))[0]
                if ticket:
                    self._start_typing(to, ticket)
            return

        self._stop_typing(to)
        ctx_token = self._context_tokens.get(to)
        if not ctx_token:
            logger.warning("WeChat: no context_token for {}, cannot reply", to)
            return

        text = _strip_markdown(msg.content.strip()) if msg.content else ""

        # Send media files if present
        if msg.media:
            for file_path in msg.media:
                try:
                    await self._send_media_file(file_path, to, ctx_token, text)
                    text = ""  # caption only on the first media
                except Exception as e:
                    logger.error("WeChat: media send failed {}: {}", file_path, e)

        # Send remaining text (or standalone text if no media)
        if text:
            try:
                await send_message(
                    base_url=self._account.base_url,
                    token=self._account.token,
                    to_user_id=to,
                    text=text,
                    context_token=ctx_token,
                )
                logger.debug("WeChat: text sent to {}", to)
            except Exception as e:
                logger.error("WeChat: send failed to {}: {}", to, e)

    async def _send_media_file(
        self, file_path: str, to: str, ctx_token: str, caption: str
    ) -> None:
        """Upload a local file to CDN and send as a media message."""
        import mimetypes
        from base64 import b64encode as _b64

        mime, _ = mimetypes.guess_type(file_path)
        mime = mime or "application/octet-stream"

        if mime.startswith("image/"):
            media_type = 1  # IMAGE
        elif mime.startswith("video/"):
            media_type = 2  # VIDEO
        else:
            media_type = 3  # FILE

        cdn_base = (
            self._cfg.get("cdnBaseUrl", CDN_BASE_URL) if self._cfg else CDN_BASE_URL
        )
        uploaded = await upload_cdn_file(
            base_url=self._account.base_url,
            token=self._account.token,
            cdn_base_url=cdn_base,
            file_path=file_path,
            to_user_id=to,
            media_type=media_type,
        )

        # aes_key in message: base64 of the hex string (matches upstream protocol)
        aes_key_b64 = _b64(uploaded["aeskey"].encode()).decode()
        media_ref = {
            "encrypt_query_param": uploaded["download_param"],
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        }

        if media_type == 1:
            item = {"type": 2, "image_item": {"media": media_ref, "mid_size": uploaded["filesize_cipher"]}}
        elif media_type == 2:
            item = {"type": 5, "video_item": {"media": media_ref, "video_size": uploaded["filesize_cipher"]}}
        else:
            fname = os.path.basename(file_path)
            item = {"type": 4, "file_item": {"media": media_ref, "file_name": fname, "len": str(uploaded["filesize_raw"])}}

        await send_media_message(
            base_url=self._account.base_url,
            token=self._account.token,
            to_user_id=to,
            context_token=ctx_token,
            media_item=item,
            text=caption,
        )
        logger.info("WeChat: media sent to {} type={} file={}", to, mime, file_path)

    # ── typing indicator ──────────────────────────────────────────────────

    async def _get_typing_ticket(self, user_id: str, context_token: str) -> str:
        """Return a cached typing_ticket, refreshing from getconfig when stale."""
        cached = self._typing_tickets.get(user_id)
        if cached:
            ticket, fetched_at = cached
            if time.monotonic() - fetched_at < _CONFIG_CACHE_TTL_S:
                return ticket

        if not self._account or not self._account.configured:
            return ""
        try:
            resp = await get_config(
                base_url=self._account.base_url,
                token=self._account.token,
                ilink_user_id=user_id,
                context_token=context_token,
            )
            if resp.get("ret", 0) == 0:
                ticket = resp.get("typing_ticket", "")
                self._typing_tickets[user_id] = (ticket, time.monotonic())
                logger.debug("WeChat: typing_ticket cached for {}", user_id)
                return ticket
        except Exception as e:
            logger.debug("WeChat: getconfig failed for {}: {}", user_id, e)
        return self._typing_tickets.get(user_id, ("", 0))[0]

    async def _typing_keepalive(self, user_id: str, ticket: str) -> None:
        """Send TYPING immediately then repeat every _TYPING_KEEPALIVE_S until cancelled."""
        if not self._account or not ticket:
            return
        try:
            await send_typing(
                self._account.base_url, self._account.token,
                user_id, ticket, TYPING_STATUS_TYPING,
            )
            while True:
                await asyncio.sleep(_TYPING_KEEPALIVE_S)
                await send_typing(
                    self._account.base_url, self._account.token,
                    user_id, ticket, TYPING_STATUS_TYPING,
                )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("WeChat: typing keepalive error for {}: {}", user_id, e)

    def _start_typing(self, user_id: str, ticket: str) -> None:
        """Start the typing indicator for a user (cancels any previous one)."""
        self._stop_typing_silent(user_id)
        if ticket:
            self._typing_tasks[user_id] = asyncio.create_task(
                self._typing_keepalive(user_id, ticket)
            )

    def _stop_typing_silent(self, user_id: str) -> bool:
        """Cancel the keepalive task without sending CANCEL. Returns True if was active."""
        task = self._typing_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def _stop_typing(self, user_id: str) -> None:
        """Cancel the keepalive task and send CANCEL to the server (only if was active)."""
        if not self._stop_typing_silent(user_id):
            return
        if self._account and self._account.configured:
            ticket = self._typing_tickets.get(user_id, ("", 0))[0]
            if ticket:
                asyncio.create_task(self._send_typing_cancel(user_id, ticket))

    async def _send_typing_cancel(self, user_id: str, ticket: str) -> None:
        try:
            await send_typing(
                self._account.base_url, self._account.token,
                user_id, ticket, TYPING_STATUS_CANCEL,
            )
        except Exception as e:
            logger.debug("WeChat: typing cancel error for {}: {}", user_id, e)

    # ── long-poll loop ───────────────────────────────────────────────────

    async def _poll_loop(self, account: AccountData) -> None:
        buf = load_sync_buf(account.account_id)
        logger.info(
            "WeChat: poll_loop starting, has_sync_buf={}",
            bool(buf),
        )
        failures = 0

        while self._running:
            try:
                resp = await get_updates(
                    base_url=account.base_url,
                    token=account.token,
                    get_updates_buf=buf,
                )

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)

                if ret != 0 or errcode != 0:
                    errmsg = resp.get("errmsg", "")
                    if errcode == _SESSION_EXPIRED or ret == _SESSION_EXPIRED:
                        # Try clearing stale sync_buf first
                        if buf:
                            logger.warning("WeChat: session expired, clearing sync_buf and retrying...")
                            buf = ""
                            save_sync_buf(account.account_id, "")
                            await asyncio.sleep(1)
                            continue

                        # Try reloading account from disk (user may have re-logged in)
                        refreshed = get_default_account()
                        if refreshed and refreshed.configured and refreshed.token != account.token:
                            logger.info(
                                "WeChat: detected new token on disk ({}...), hot-reloading",
                                refreshed.token[:12],
                            )
                            account = refreshed
                            self._account = refreshed
                            buf = ""
                            continue

                        logger.error(
                            "WeChat: session expired (ret={} errcode={} errmsg={}), "
                            "pausing {}s. Re-login: nanobot-weixin login",
                            ret, errcode, errmsg, _SESSION_PAUSE_S,
                        )
                        await self._sleep(_SESSION_PAUSE_S)
                        failures = 0
                        continue
                    failures += 1
                    logger.warning(
                        "WeChat: getUpdates error ret={} errcode={} errmsg={} ({}/{})",
                        ret, errcode, errmsg, failures, _MAX_FAILURES,
                    )
                    if failures >= _MAX_FAILURES:
                        failures = 0
                        await self._sleep(_BACKOFF_S)
                    else:
                        await self._sleep(_RETRY_S)
                    continue

                failures = 0
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    save_sync_buf(account.account_id, new_buf)
                    buf = new_buf

                for raw_msg in resp.get("msgs", []):
                    await self._process_inbound(account, raw_msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                failures += 1
                logger.error("WeChat: poll error ({}/{}): {}", failures, _MAX_FAILURES, e)
                if failures >= _MAX_FAILURES:
                    failures = 0
                    await self._sleep(_BACKOFF_S)
                else:
                    await self._sleep(_RETRY_S)

    async def _sleep(self, seconds: float) -> None:
        try:
            for _ in range(int(seconds)):
                if not self._running:
                    return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    # ── inbound message processing ───────────────────────────────────────

    async def _process_inbound(self, account: AccountData, msg: dict[str, Any]) -> None:
        from_user = msg.get("from_user_id", "")
        if msg.get("message_type", 0) != 1 or not from_user:
            return

        ctx_token = msg.get("context_token", "")
        if ctx_token:
            self._context_tokens[from_user] = ctx_token

        items = msg.get("item_list", [])
        parts: list[str] = []
        media: list[str] = []

        for item in items:
            t = item.get("type", 0)

            if t == 1:  # TEXT
                text = (item.get("text_item") or {}).get("text", "")
                if not text:
                    continue
                ref = item.get("ref_msg")
                if ref and ref.get("title"):
                    parts.append(f"[quote: {ref['title']}]\n{text}")
                else:
                    parts.append(text)

            elif t == 2:  # IMAGE
                path = await self._download_media(account, item.get("image_item", {}), "image")
                if path:
                    media.append(path)
                    parts.append(f"[image: {os.path.basename(path)}]")

            elif t == 3:  # VOICE
                voice = item.get("voice_item", {})
                voice_text = voice.get("text", "")
                if voice_text:
                    parts.append(f"[voice] {voice_text}")
                else:
                    path = await self._download_media(account, voice, "voice")
                    if path:
                        media.append(path)
                        parts.append(f"[voice: {os.path.basename(path)}]")

            elif t == 4:  # FILE
                fi = item.get("file_item", {})
                fname = fi.get("file_name", "file")
                path = await self._download_media(account, fi, "file", fname)
                if path:
                    media.append(path)
                    parts.append(f"[file: {fname}]\n[File: source: {path}]")

            elif t == 5:  # VIDEO
                path = await self._download_media(account, item.get("video_item", {}), "video")
                if path:
                    media.append(path)
                    parts.append(f"[video: {os.path.basename(path)}]")

        content = "\n".join(parts)
        if not content:
            return

        ticket = await self._get_typing_ticket(from_user, ctx_token)
        self._start_typing(from_user, ticket)

        await self._handle_message(
            sender_id=from_user,
            chat_id=from_user,
            content=content,
            media=media or None,
            metadata={
                "account_id": account.account_id,
                "message_id": str(msg.get("message_id", "")),
                "context_token": ctx_token,
            },
        )

    async def _download_media(
        self,
        account: AccountData,
        info: dict[str, Any],
        kind: str,
        filename: str | None = None,
    ) -> str | None:
        """Download + AES decrypt a CDN media item. Returns local path."""
        ref = info.get("media", {})
        param = ref.get("encrypt_query_param", "")
        aes_key = info.get("aeskey", "") or ref.get("aes_key", "")

        if not param or not aes_key:
            return None

        # aes_key can be 32-char hex or base64
        if len(aes_key) == 32 and all(c in "0123456789abcdefABCDEF" for c in aes_key):
            key_hex = aes_key
        else:
            from base64 import b64decode
            key_hex = b64decode(aes_key).hex()

        try:
            cdn = self._cfg.get("cdnBaseUrl", CDN_BASE_URL) if self._cfg else CDN_BASE_URL
            data = await download_cdn_media(cdn, param, key_hex)

            out_dir = _media_dir()
            if not filename:
                ext = {"image": ".jpg", "voice": ".silk", "video": ".mp4", "file": ""}.get(kind, "")
                filename = f"{kind}_{secrets.token_hex(6)}{ext}"

            path = os.path.join(out_dir, filename)
            with open(path, "wb") as f:
                f.write(data)
            logger.debug("WeChat: downloaded {} → {}", kind, path)
            return path

        except Exception as e:
            logger.error("WeChat: media download failed: {}", e)
            return None

