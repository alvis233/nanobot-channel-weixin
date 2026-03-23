"""WeChat QR-code login and credential storage."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot_channel_weixin.api import DEFAULT_BASE_URL, fetch_qr_code, poll_qr_status

_MAX_QR_REFRESHES = 3
_LOGIN_TIMEOUT_S = 480


# ── Paths ────────────────────────────────────────────────────────────────


def _state_dir() -> Path:
    try:
        from nanobot.config.paths import get_runtime_subdir
        d = get_runtime_subdir("state") / "weixin"
    except Exception:
        d = Path.home() / ".nanobot" / "state" / "weixin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _accounts_dir() -> Path:
    d = _state_dir() / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sync_dir() -> Path:
    d = _state_dir() / "sync"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_account_id(raw: str) -> str:
    """'abc123@im.bot' → 'abc123-im-bot' (filesystem-safe)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", raw)


# ── Account index ────────────────────────────────────────────────────────


def _index_path() -> Path:
    return _state_dir() / "accounts.json"


def list_account_ids() -> list[str]:
    p = _index_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return [s for s in data if isinstance(s, str) and s.strip()]
    except Exception:
        return []


def _register_account_id(account_id: str) -> None:
    old_ids = list_account_ids()
    # Remove stale account files from previous logins
    for old_id in old_ids:
        if old_id != account_id:
            old_file = _account_path(old_id)
            if old_file.exists():
                old_file.unlink()
                logger.debug("Removed stale account file: {}", old_id)
            old_buf = _sync_dir() / f"{old_id}.buf"
            if old_buf.exists():
                old_buf.unlink()
    # Only keep the current account
    _index_path().write_text(json.dumps([account_id], indent=2))


# ── Account data ─────────────────────────────────────────────────────────


class AccountData:
    """Credential store for one WeChat bot account."""

    def __init__(
        self,
        account_id: str,
        token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        user_id: str = "",
    ):
        self.account_id = account_id
        self.token = token
        self.base_url = base_url
        self.user_id = user_id

    @property
    def configured(self) -> bool:
        return bool(self.token)


def _account_path(account_id: str) -> Path:
    return _accounts_dir() / f"{account_id}.json"


def load_account(account_id: str) -> AccountData | None:
    p = _account_path(account_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return AccountData(
            account_id=account_id,
            token=d.get("token", ""),
            base_url=d.get("baseUrl", DEFAULT_BASE_URL),
            user_id=d.get("userId", ""),
        )
    except Exception:
        return None


def save_account(account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    p = _account_path(account_id)
    data: dict[str, str] = {"token": token, "baseUrl": base_url}
    if user_id:
        data["userId"] = user_id
    p.write_text(json.dumps(data, indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    _register_account_id(account_id)
    # Clear stale sync buf so new session starts fresh
    buf_path = _sync_dir() / f"{account_id}.buf"
    if buf_path.exists():
        buf_path.unlink()
        logger.info("Cleared stale sync buf for {}", account_id)


def get_default_account() -> AccountData | None:
    """Return the current account (only one is kept after each login)."""
    ids = list_account_ids()
    if not ids:
        return None
    acct = load_account(ids[0])
    return acct if acct and acct.configured else None


# ── Sync buf persistence ─────────────────────────────────────────────────


def load_sync_buf(account_id: str) -> str:
    p = _sync_dir() / f"{account_id}.buf"
    return p.read_text() if p.exists() else ""


def save_sync_buf(account_id: str, buf: str) -> None:
    (_sync_dir() / f"{account_id}.buf").write_text(buf)


# ── QR login flow ────────────────────────────────────────────────────────


async def login_with_qr(
    base_url: str = DEFAULT_BASE_URL,
    print_fn: Any = print,
) -> AccountData | None:
    """Interactive QR login. Returns AccountData on success."""
    print_fn("Fetching QR code from WeChat...")
    qr_resp = await fetch_qr_code(base_url)
    qr_token = qr_resp.get("qrcode", "")
    qr_url = qr_resp.get("qrcode_img_content", "")

    if not qr_token or not qr_url:
        print_fn("Failed to get QR code from server.")
        return None

    _print_qr(qr_url, print_fn)

    scanned_shown = False
    refresh_count = 1
    deadline = asyncio.get_event_loop().time() + _LOGIN_TIMEOUT_S

    while asyncio.get_event_loop().time() < deadline:
        status_resp = await poll_qr_status(qr_token, base_url)
        status = status_resp.get("status", "wait")

        if status == "wait":
            continue

        if status == "scaned" and not scanned_shown:
            print_fn("\nScanned! Confirm on your phone...")
            scanned_shown = True

        elif status == "expired":
            refresh_count += 1
            if refresh_count > _MAX_QR_REFRESHES:
                print_fn("QR code expired too many times.")
                return None
            print_fn(f"\nQR expired, refreshing ({refresh_count}/{_MAX_QR_REFRESHES})...")
            qr_resp = await fetch_qr_code(base_url)
            qr_token = qr_resp.get("qrcode", "")
            qr_url = qr_resp.get("qrcode_img_content", "")
            scanned_shown = False
            _print_qr(qr_url, print_fn)

        elif status == "confirmed":
            bot_token = status_resp.get("bot_token", "")
            bot_id = status_resp.get("ilink_bot_id", "")
            srv_url = status_resp.get("baseurl", "") or base_url
            user_id = status_resp.get("ilink_user_id", "")

            if not bot_id:
                print_fn("Login confirmed but server returned no bot ID.")
                return None

            aid = normalize_account_id(bot_id)
            logger.info(
                "WeChat login OK: bot_id={} account_id={} base_url={} token={}... user_id={}",
                bot_id, aid, srv_url, bot_token[:12] if bot_token else "NONE", user_id,
            )
            save_account(aid, bot_token, srv_url, user_id)
            return load_account(aid)

        await asyncio.sleep(1)

    print_fn("Login timed out.")
    return None


def _print_qr(url: str, print_fn: Any) -> None:
    print_fn("\nScan the QR code below with WeChat:\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print_fn(f"QR URL: {url}")
