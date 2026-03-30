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
    download_cdn_media_plain,
    get_config,
    get_updates,
    send_media_message,
    send_message,
    send_typing,
    upload_cdn_file,
)
from nanobot_channel_weixin.auth import (
    AccountData,
    load_account,
    load_all_accounts,
    load_sync_buf,
    remove_account,
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
_ACCOUNT_SCAN_INTERVAL_S = 10
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
    """Resolve the weixin-community media directory.

    Uses nanobot's configured media path (~/.nanobot/media/weixin-community/) when running
    inside the gateway. Falls back to /tmp/nanobot/media/weixin-community/ when nanobot
    config is unavailable (e.g. standalone CLI testing).
    """
    try:
        from nanobot.config.paths import get_media_dir
        return str(get_media_dir("weixin-community"))
    except Exception:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "nanobot", "media", "weixin-community")
        os.makedirs(d, exist_ok=True)
        return d


class WeixinChannel(BaseChannel):
    """
    Personal WeChat channel.

    Login:   nanobot-weixin login
    Config:  channels.weixin-community.enabled = true  in ~/.nanobot/config.json
    """

    name = "weixin-community"
    display_name = "WeChat (Community)"

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
        self._accounts: dict[str, AccountData] = {}
        # All per-session state uses (account_id, user_id) composite keys
        # so multiple accounts never interfere with each other.
        self._context_tokens: dict[tuple[str, str], str] = {}
        self._typing_tickets: dict[tuple[str, str], tuple[str, float]] = {}
        self._typing_tasks: dict[tuple[str, str], asyncio.Task] = {}  # type: ignore[type-arg]

    async def start(self) -> None:
        """Start long-poll monitors for all configured accounts.

        Also starts a background watcher that detects accounts added
        after the gateway is already running (e.g. via ``nanobot-weixin login``).
        """
        self._running = True
        self._poll_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

        for acct in load_all_accounts():
            self._start_poll(acct)

        if not self._accounts:
            logger.error(
                "WeChat: no configured account. Run: nanobot-weixin login"
            )

        watcher = asyncio.create_task(self._account_watcher())
        try:
            while self._running:
                # Keep alive; individual poll_loops run as tracked tasks
                await self._sleep(1)
        finally:
            watcher.cancel()
            for t in self._poll_tasks.values():
                t.cancel()

    def _start_poll(self, acct: AccountData) -> None:
        """Register an account and launch its poll_loop task."""
        self._accounts[acct.account_id] = acct
        self._poll_tasks[acct.account_id] = asyncio.create_task(
            self._poll_loop(acct)
        )
        logger.info(
            "WeChat channel starting: account={} base_url={}",
            acct.account_id, acct.base_url,
        )

    async def _account_watcher(self) -> None:
        """Periodically scan disk for newly added accounts and start polling them."""
        while self._running:
            await self._sleep(_ACCOUNT_SCAN_INTERVAL_S)
            try:
                for acct in load_all_accounts():
                    if acct.account_id not in self._accounts:
                        logger.info(
                            "WeChat: detected new account on disk, starting poll: {}",
                            acct.account_id,
                        )
                        self._start_poll(acct)
            except Exception as e:
                logger.debug("WeChat: account watcher scan error: {}", e)

    async def stop(self) -> None:
        self._running = False
        logger.info("WeChat channel stopped")

    def _resolve_send_target(
        self, msg: OutboundMessage,
    ) -> tuple[AccountData, str] | None:
        """Extract the account and real peer user_id from an outbound message.

        chat_id format is "owner:peer" where owner is account.user_id
        (stable WeChat identity).  We also check metadata["account_id"]
        for a direct account_id match.
        """
        chat_id = msg.chat_id
        if ":" in chat_id:
            owner, peer = chat_id.split(":", 1)
        else:
            owner = ""
            peer = chat_id

        # Try metadata account_id first (always exact)
        acct_id = msg.metadata.get("account_id", "")
        if acct_id and acct_id in self._accounts:
            return self._accounts[acct_id], peer

        # Match by owner (account.user_id)
        if owner:
            for acct in self._accounts.values():
                if acct.user_id == owner or acct.account_id == owner:
                    return acct, peer

        if len(self._accounts) == 1:
            return next(iter(self._accounts.values())), peer
        return None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a reply back through WeChat (text and/or media)."""
        result = self._resolve_send_target(msg)
        if not result:
            logger.warning("WeChat: cannot send, no matching account for msg")
            return

        account, to = result
        key = (account.account_id, to)

        if msg.metadata.get("_progress"):
            task = self._typing_tasks.get(key)
            if not task or task.done():
                ticket = self._typing_tickets.get(key, ("", 0))[0]
                if ticket:
                    self._start_typing(key, account, ticket)
            return

        self._stop_typing(key, account)
        ctx_token = self._context_tokens.get(key)
        if not ctx_token:
            logger.warning("WeChat: no context_token for {}, cannot reply", key)
            return

        text = _strip_markdown(msg.content.strip()) if msg.content else ""

        if msg.media:
            for file_path in msg.media:
                try:
                    await self._send_media_file(account, file_path, to, ctx_token, text)
                    text = ""
                except Exception as e:
                    logger.error("WeChat: media send failed {}: {}", file_path, e)

        if text:
            try:
                await send_message(
                    base_url=account.base_url,
                    token=account.token,
                    to_user_id=to,
                    text=text,
                    context_token=ctx_token,
                )
                logger.debug("WeChat: text sent to {}", to)
            except Exception as e:
                logger.error("WeChat: send failed to {}: {}", to, e)

    async def _send_media_file(
        self, account: AccountData, file_path: str, to: str, ctx_token: str, caption: str
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
            base_url=account.base_url,
            token=account.token,
            cdn_base_url=cdn_base,
            file_path=file_path,
            to_user_id=to,
            media_type=media_type,
        )

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
            base_url=account.base_url,
            token=account.token,
            to_user_id=to,
            context_token=ctx_token,
            media_item=item,
            text=caption,
        )
        logger.info("WeChat: media sent to {} type={} file={}", to, mime, file_path)

    # ── typing indicator ──────────────────────────────────────────────────

    async def _get_typing_ticket(
        self, key: tuple[str, str], account: AccountData, context_token: str,
    ) -> str:
        """Return a cached typing_ticket, refreshing from getconfig when stale."""
        cached = self._typing_tickets.get(key)
        if cached:
            ticket, fetched_at = cached
            if time.monotonic() - fetched_at < _CONFIG_CACHE_TTL_S:
                return ticket

        user_id = key[1]
        try:
            resp = await get_config(
                base_url=account.base_url,
                token=account.token,
                ilink_user_id=user_id,
                context_token=context_token,
            )
            if resp.get("ret", 0) == 0:
                ticket = resp.get("typing_ticket", "")
                self._typing_tickets[key] = (ticket, time.monotonic())
                logger.debug("WeChat: typing_ticket cached for {}", key)
                return ticket
        except Exception as e:
            logger.debug("WeChat: getconfig failed for {}: {}", key, e)
        return self._typing_tickets.get(key, ("", 0))[0]

    async def _typing_keepalive(
        self, account: AccountData, user_id: str, ticket: str,
    ) -> None:
        """Send TYPING immediately then repeat every _TYPING_KEEPALIVE_S until cancelled."""
        if not ticket:
            return
        try:
            await send_typing(
                account.base_url, account.token,
                user_id, ticket, TYPING_STATUS_TYPING,
            )
            while True:
                await asyncio.sleep(_TYPING_KEEPALIVE_S)
                await send_typing(
                    account.base_url, account.token,
                    user_id, ticket, TYPING_STATUS_TYPING,
                )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("WeChat: typing keepalive error for {}: {}", user_id, e)

    def _start_typing(
        self, key: tuple[str, str], account: AccountData, ticket: str,
    ) -> None:
        """Start the typing indicator (cancels any previous one for same key)."""
        self._stop_typing_silent(key)
        if ticket:
            self._typing_tasks[key] = asyncio.create_task(
                self._typing_keepalive(account, key[1], ticket)
            )

    def _stop_typing_silent(self, key: tuple[str, str]) -> bool:
        """Cancel the keepalive task without sending CANCEL. Returns True if was active."""
        task = self._typing_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def _stop_typing(self, key: tuple[str, str], account: AccountData) -> None:
        """Cancel the keepalive task and send CANCEL to the server (only if was active)."""
        if not self._stop_typing_silent(key):
            return
        ticket = self._typing_tickets.get(key, ("", 0))[0]
        if ticket:
            asyncio.create_task(self._send_typing_cancel(account, key[1], ticket))

    async def _send_typing_cancel(
        self, account: AccountData, user_id: str, ticket: str,
    ) -> None:
        try:
            await send_typing(
                account.base_url, account.token,
                user_id, ticket, TYPING_STATUS_CANCEL,
            )
        except Exception as e:
            logger.debug("WeChat: typing cancel error for {}: {}", user_id, e)

    # ── long-poll loop ───────────────────────────────────────────────────

    def _find_successor(
        self, old_account_id: str, old_user_id: str,
    ) -> AccountData | None:
        """Find a successor account belonging to the same WeChat user.

        When a WeChat re-login generates a new bot_id, the old poll_loop
        needs to discover and adopt the new account.  We match by user_id
        (the stable WeChat identity) to avoid cross-user misattribution.
        """
        if not old_user_id:
            logger.warning(
                "WeChat [{}]: no user_id on record, cannot search for successor",
                old_account_id,
            )
            return None
        candidates = load_all_accounts()
        for acct in candidates:
            if acct.account_id == old_account_id or acct.account_id in self._accounts:
                continue
            if acct.user_id == old_user_id:
                return acct
            logger.debug(
                "WeChat [{}]: candidate [{}] skipped, user_id mismatch",
                old_account_id, acct.account_id,
            )
        return None

    async def _poll_loop(self, account: AccountData) -> None:
        buf = load_sync_buf(account.account_id)
        logger.info(
            "WeChat: poll_loop starting account={}, has_sync_buf={}",
            account.account_id, bool(buf),
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
                        if buf:
                            logger.warning(
                                "WeChat [{}]: session expired, clearing sync_buf and retrying...",
                                account.account_id,
                            )
                            buf = ""
                            save_sync_buf(account.account_id, "")
                            await asyncio.sleep(1)
                            continue

                        # Same account_id, new token (user re-logged same bot_id)
                        refreshed = load_account(account.account_id)
                        if refreshed and refreshed.configured and refreshed.token != account.token:
                            logger.info(
                                "WeChat [{}]: detected new token on disk, hot-reloading",
                                account.account_id,
                            )
                            account = refreshed
                            self._accounts[account.account_id] = refreshed
                            buf = ""
                            continue

                        # Different account_id: WeChat re-login often generates
                        # a new bot_id.  Scan for any new accounts that appeared
                        # on disk belonging to the same WeChat user.
                        successor = self._find_successor(account.account_id, account.user_id)
                        if successor:
                            logger.info(
                                "WeChat [{}]: migrating to successor account [{}], user_id={}",
                                account.account_id, successor.account_id, account.user_id,
                            )
                            old_id = account.account_id
                            self._accounts.pop(old_id, None)
                            remove_account(old_id)
                            account = successor
                            self._accounts[successor.account_id] = successor
                            buf = ""
                            continue

                        logger.error(
                            "WeChat [{}]: session expired (ret={} errcode={} errmsg={}), "
                            "pausing {}s. Re-login: nanobot-weixin login",
                            account.account_id, ret, errcode, errmsg, _SESSION_PAUSE_S,
                        )
                        await self._sleep(_SESSION_PAUSE_S)
                        failures = 0
                        continue
                    failures += 1
                    logger.warning(
                        "WeChat [{}]: getUpdates error ret={} errcode={} errmsg={} ({}/{})",
                        account.account_id, ret, errcode, errmsg, failures, _MAX_FAILURES,
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
                logger.error(
                    "WeChat [{}]: poll error ({}/{}): {}",
                    account.account_id, failures, _MAX_FAILURES, e,
                )
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

        key = (account.account_id, from_user)
        ctx_token = msg.get("context_token", "")
        if ctx_token:
            self._context_tokens[key] = ctx_token

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

        ticket = await self._get_typing_ticket(key, account, ctx_token)
        self._start_typing(key, account, ticket)

        # Use account.user_id (stable WeChat identity) instead of account_id
        # (which changes on every re-login) so conversation history survives
        # re-login while still isolating different WeChat accounts.
        owner = account.user_id or account.account_id
        chat_id = f"{owner}:{from_user}"

        await self._handle_message(
            sender_id=from_user,
            chat_id=chat_id,
            content=content,
            media=media or None,
            metadata={
                "account_id": account.account_id,
                "message_id": str(msg.get("message_id", "")),
                "context_token": ctx_token,
            },
        )

    def _parse_aes_key(self, info: dict[str, Any], kind: str) -> str | None:
        """Parse AES key from media item, returning hex string or None.

        Matches the official SDK's parseAesKey logic:
        - For images: image_item.aeskey is a 32-char hex string → convert to
          raw 16 bytes → use as AES key.
        - For other types: media.aes_key is base64-encoded, decoding to either
          16 raw bytes or 32-char hex string (which must be hex-decoded again).
        - Returns the AES key as a 32-char hex string suitable for bytes.fromhex().
        """
        from base64 import b64decode, b64encode

        ref = info.get("media", {})

        # For images: prefer image_item.aeskey (raw hex) over media.aes_key.
        # Convert hex aeskey → raw bytes → base64 for uniform handling.
        raw_hex = info.get("aeskey", "")
        if raw_hex:
            aes_key_b64 = b64encode(bytes.fromhex(raw_hex)).decode()
        else:
            aes_key_b64 = ref.get("aes_key", "")

        if not aes_key_b64:
            return None

        # parseAesKey: base64 → raw bytes.
        # Two encodings in the wild:
        #   - base64(raw 16 bytes)           → images
        #   - base64(hex string of 16 bytes) → file / voice / video
        decoded = b64decode(aes_key_b64)
        if len(decoded) == 16:
            return decoded.hex()
        if len(decoded) == 32:
            try:
                hex_str = decoded.decode("ascii")
                if all(c in "0123456789abcdefABCDEF" for c in hex_str):
                    return bytes.fromhex(hex_str).hex()
            except (UnicodeDecodeError, ValueError):
                pass
        logger.warning(
            "WeChat: unexpected aes_key length after b64decode: {} bytes (kind={})",
            len(decoded), kind,
        )
        return decoded.hex()

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
        full_url = ref.get("full_url", "")

        if not param and not full_url:
            return None

        cdn = self._cfg.get("cdnBaseUrl", CDN_BASE_URL) if self._cfg else CDN_BASE_URL

        try:
            key_hex = self._parse_aes_key(info, kind)
            if key_hex:
                data = await download_cdn_media(cdn, param, key_hex, full_url or None)
            else:
                data = await download_cdn_media_plain(cdn, param, full_url or None)

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

