"""Standalone CLI for WeChat channel plugin: nanobot-weixin login"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from nanobot_channel_weixin.api import DEFAULT_BASE_URL
from nanobot_channel_weixin.auth import get_default_account, list_account_ids, load_account, login_with_qr


def _resolve_config_path() -> Path:
    """Return the nanobot config path, respecting multi-instance setup."""
    try:
        from nanobot.config.loader import get_config_path
        return get_config_path()
    except Exception:
        return Path.home() / ".nanobot" / "config.json"


def _load_base_url() -> str:
    """Read baseUrl from nanobot config if available."""
    cfg_path = _resolve_config_path()
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            return cfg.get("channels", {}).get("weixin", {}).get("baseUrl", DEFAULT_BASE_URL)
        except Exception:
            pass
    return DEFAULT_BASE_URL


def _enable_in_config() -> None:
    """Auto-set channels.weixin.enabled = true in config.json."""
    cfg_path = _resolve_config_path()
    if not cfg_path.exists():
        return
    try:
        cfg = json.loads(cfg_path.read_text())
        weixin = cfg.setdefault("channels", {}).setdefault("weixin", {})
        if not weixin.get("enabled"):
            weixin["enabled"] = True
            weixin.setdefault("allowFrom", ["*"])
            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            print(f"  Auto-enabled weixin channel in {cfg_path}")
    except Exception:
        pass


def cmd_login() -> None:
    base_url = _load_base_url()
    print("🔗 WeChat Login for nanobot")
    print("Scan the QR code with your WeChat app.\n")

    async def _run():
        account = await login_with_qr(base_url=base_url)
        if account:
            print(f"\n✅ WeChat connected!  account={account.account_id}")
            _enable_in_config()
            print("\nStart the gateway to begin chatting:")
            print("  nanobot gateway")
        else:
            print("\n❌ WeChat login failed")
            sys.exit(1)

    asyncio.run(_run())


def cmd_status() -> None:
    ids = list_account_ids()
    if not ids:
        print("No WeChat accounts configured.")
        print("Run: nanobot-weixin login")
        return
    print(f"WeChat accounts ({len(ids)}):\n")
    for aid in ids:
        acct = load_account(aid)
        status = "✅ configured" if acct and acct.configured else "❌ not configured"
        user = f"  user={acct.user_id}" if acct and acct.user_id else ""
        print(f"  {aid}: {status}{user}")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print("Usage: nanobot-weixin <command>\n")
        print("Commands:")
        print("  login    Scan QR code to connect your WeChat account")
        print("  status   Show configured WeChat accounts")
        return

    cmd = args[0]
    if cmd == "login":
        cmd_login()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"Unknown command: {cmd}")
        print("Run: nanobot-weixin --help")
        sys.exit(1)
