"""Personal WeChat channel for nanobot via iLink Bot long-poll API."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

from nanobot_channel_weixin.api import (
    CDN_BASE_URL,
    DEFAULT_BASE_URL,
    download_cdn_media,
    get_updates,
    send_message,
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
    """Resolve the weixin media directory (works with or without nanobot config)."""
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
        """Send a reply back through WeChat."""
        if not self._account or not self._account.configured:
            logger.warning("WeChat: cannot send, account not configured")
            return

        text = _strip_markdown(msg.content.strip())
        if not text:
            return

        to = msg.chat_id
        ctx_token = self._context_tokens.get(to)
        if not ctx_token:
            logger.warning("WeChat: no context_token for {}, cannot reply", to)
            return

        try:
            await send_message(
                base_url=self._account.base_url,
                token=self._account.token,
                to_user_id=to,
                text=text,
                context_token=ctx_token,
            )
            logger.debug("WeChat: sent to {}", to)
        except Exception as e:
            logger.error("WeChat: send failed to {}: {}", to, e)

    # ── long-poll loop ───────────────────────────────────────────────────

    async def _poll_loop(self, account: AccountData) -> None:
        buf = load_sync_buf(account.account_id)
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
                    if errcode == _SESSION_EXPIRED or ret == _SESSION_EXPIRED:
                        logger.error("WeChat: session expired, pausing {}s", _SESSION_PAUSE_S)
                        await self._sleep(_SESSION_PAUSE_S)
                        failures = 0
                        continue
                    failures += 1
                    logger.warning(
                        "WeChat: getUpdates error ret={} errcode={} ({}/{})",
                        ret, errcode, failures, _MAX_FAILURES,
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
