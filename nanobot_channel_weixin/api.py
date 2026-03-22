"""HTTP API client for the WeChat iLink Bot service."""

from __future__ import annotations

import hashlib
import secrets
from base64 import b64encode
from typing import Any

import httpx
from loguru import logger

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

LONG_POLL_TIMEOUT_S = 35
API_TIMEOUT_S = 15


def _random_wechat_uin() -> str:
    """X-WECHAT-UIN header: random uint32 → decimal → base64."""
    n = int.from_bytes(secrets.token_bytes(4), "big")
    return b64encode(str(n).encode()).decode()


def _build_headers(token: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _trailing(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


# ── QR login ─────────────────────────────────────────────────────────────


async def fetch_qr_code(
    base_url: str = DEFAULT_BASE_URL,
    bot_type: str = "3",
) -> dict[str, str]:
    """GET ilink/bot/get_bot_qrcode → {qrcode, qrcode_img_content}."""
    url = f"{_trailing(base_url)}ilink/bot/get_bot_qrcode"
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
        r = await c.get(url, params={"bot_type": bot_type})
        r.raise_for_status()
        return r.json()


async def poll_qr_status(
    qrcode: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = LONG_POLL_TIMEOUT_S,
) -> dict[str, Any]:
    """Long-poll GET ilink/bot/get_qrcode_status."""
    url = f"{_trailing(base_url)}ilink/bot/get_qrcode_status"
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 5) as c:
            r = await c.get(
                url,
                params={"qrcode": qrcode},
                headers={"iLink-App-ClientVersion": "1"},
                timeout=timeout_s + 5,
            )
            r.raise_for_status()
            return r.json()
    except httpx.ReadTimeout:
        return {"status": "wait"}


# ── Message API ──────────────────────────────────────────────────────────


async def get_updates(
    base_url: str,
    token: str,
    get_updates_buf: str = "",
    timeout_s: float = LONG_POLL_TIMEOUT_S,
) -> dict[str, Any]:
    """Long-poll POST getupdates → {ret, msgs[], get_updates_buf}."""
    url = f"{_trailing(base_url)}ilink/bot/getupdates"
    body = {"get_updates_buf": get_updates_buf, "base_info": {}}
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 5) as c:
            r = await c.post(url, json=body, headers=_build_headers(token), timeout=timeout_s + 5)
            r.raise_for_status()
            return r.json()
    except httpx.ReadTimeout:
        logger.debug("getUpdates: client-side timeout, returning empty")
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}


async def send_message(
    base_url: str,
    token: str,
    to_user_id: str,
    text: str,
    context_token: str,
    client_id: str | None = None,
) -> str:
    """POST sendmessage. Returns the client_id used."""
    url = f"{_trailing(base_url)}ilink/bot/sendmessage"
    cid = client_id or f"nanobot-{secrets.token_hex(8)}"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": cid,
            "message_type": 2,   # BOT
            "message_state": 2,  # FINISH
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": context_token,
        },
        "base_info": {},
    }
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
        r = await c.post(url, json=body, headers=_build_headers(token))
        r.raise_for_status()
    return cid


async def send_media_message(
    base_url: str,
    token: str,
    to_user_id: str,
    context_token: str,
    media_item: dict[str, Any],
    text: str = "",
) -> str:
    """Send a media message (image/video/file). Returns client_id."""
    url = f"{_trailing(base_url)}ilink/bot/sendmessage"
    items: list[dict[str, Any]] = []
    if text:
        items.append({"type": 1, "text_item": {"text": text}})
    items.append(media_item)

    cid = f"nanobot-{secrets.token_hex(8)}"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": cid,
            "message_type": 2,
            "message_state": 2,
            "item_list": items,
            "context_token": context_token,
        },
        "base_info": {},
    }
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
        r = await c.post(url, json=body, headers=_build_headers(token))
        r.raise_for_status()
    return cid


# ── CDN helpers ──────────────────────────────────────────────────────────


async def download_cdn_media(
    cdn_base_url: str,
    encrypt_query_param: str,
    aes_key_hex: str,
) -> bytes:
    """Download and AES-128-ECB decrypt a CDN file."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    url = f"{cdn_base_url}?{encrypt_query_param}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        ciphertext = r.content

    cipher = Cipher(algorithms.AES(bytes.fromhex(aes_key_hex)), modes.ECB())
    plaintext = cipher.decryptor().update(ciphertext) + cipher.decryptor().finalize()

    # PKCS7 unpad
    pad = plaintext[-1]
    if 1 <= pad <= 16 and all(b == pad for b in plaintext[-pad:]):
        plaintext = plaintext[:-pad]
    return plaintext


async def upload_cdn_file(
    base_url: str,
    token: str,
    cdn_base_url: str,
    file_path: str,
    to_user_id: str,
    media_type: int,
) -> dict[str, Any]:
    """Encrypt + upload a local file to CDN. Returns metadata for building a media message."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw_data = open(file_path, "rb").read()
    rawsize = len(raw_data)
    raw_md5 = hashlib.md5(raw_data).hexdigest()
    aes_key = secrets.token_bytes(16)

    # PKCS7 pad + AES-128-ECB encrypt
    pad_len = 16 - (rawsize % 16)
    padded = raw_data + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
    ciphertext = cipher.encryptor().update(padded) + cipher.encryptor().finalize()

    filekey = f"nanobot_{secrets.token_hex(8)}"

    # Get pre-signed upload URL
    upload_url = f"{_trailing(base_url)}ilink/bot/getuploadurl"
    upload_body: dict[str, Any] = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": rawsize,
        "rawfilemd5": raw_md5,
        "filesize": len(ciphertext),
        "aeskey": aes_key.hex(),
        "no_need_thumb": True,
        "base_info": {},
    }
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
        r = await c.post(upload_url, json=upload_body, headers=_build_headers(token))
        r.raise_for_status()
        upload_resp = r.json()

    upload_param = upload_resp.get("upload_param", "")
    if not upload_param:
        raise RuntimeError("getuploadurl returned empty upload_param")

    # PUT ciphertext to CDN
    cdn_url = f"{cdn_base_url}?{upload_param}"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(cdn_url, content=ciphertext, headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()

    return {
        "filekey": filekey,
        "aeskey": aes_key.hex(),
        "download_param": upload_param,
        "filesize_cipher": len(ciphertext),
        "filesize_raw": rawsize,
    }
