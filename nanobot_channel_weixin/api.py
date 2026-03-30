"""HTTP API client for the WeChat iLink Bot service."""

from __future__ import annotations

import hashlib
import secrets
from base64 import b64encode
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

LONG_POLL_TIMEOUT_S = 35
API_TIMEOUT_S = 15
CONFIG_TIMEOUT_S = 10

CHANNEL_VERSION = "2.1.1"
ILINK_APP_ID = "bot"
# iLink-App-ClientVersion: uint32 encoded as 0x00MMNNPP from semver
_ver = tuple(int(x) for x in CHANNEL_VERSION.split("."))
ILINK_APP_CLIENT_VERSION = str((_ver[0] << 16) | (_ver[1] << 8) | _ver[2])


def _random_wechat_uin() -> str:
    """X-WECHAT-UIN header: random uint32 → decimal → base64."""
    n = int.from_bytes(secrets.token_bytes(4), "big")
    return b64encode(str(n).encode()).decode()


def _build_headers(token: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _build_base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION}


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
    body = {"get_updates_buf": get_updates_buf, "base_info": _build_base_info()}
    logger.debug(
        "getUpdates: url={} token={}... buf_len={}",
        url, token[:12] if token else "NONE", len(get_updates_buf),
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 5) as c:
            r = await c.post(url, json=body, headers=_build_headers(token), timeout=timeout_s + 5)
            r.raise_for_status()
            data = r.json()
            ret = data.get("ret", 0)
            errcode = data.get("errcode", 0)
            msgs = data.get("msgs", [])
            buf_len = len(data.get("get_updates_buf", ""))
            logger.debug(
                "getUpdates: ret={} errcode={} msgs={} buf_len={} errmsg={}",
                ret, errcode, len(msgs), buf_len, data.get("errmsg", ""),
            )
            return data
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
        "base_info": _build_base_info(),
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
    """Send a media message (image/video/file).

    Matches upstream protocol: each item is sent as a separate API call.
    If text is provided, it is sent first, then the media item.
    """
    url = f"{_trailing(base_url)}ilink/bot/sendmessage"
    items: list[dict[str, Any]] = []
    if text:
        items.append({"type": 1, "text_item": {"text": text}})
    items.append(media_item)

    last_cid = ""
    for item in items:
        last_cid = f"nanobot-{secrets.token_hex(8)}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": last_cid,
                "message_type": 2,
                "message_state": 2,
                "item_list": [item],
                "context_token": context_token,
            },
            "base_info": _build_base_info(),
        }
        async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
            r = await c.post(url, json=body, headers=_build_headers(token))
            r.raise_for_status()
    return last_cid


# ── Typing indicator ─────────────────────────────────────────────────────

TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2


async def get_config(
    base_url: str,
    token: str,
    ilink_user_id: str,
    context_token: str = "",
) -> dict[str, Any]:
    """POST ilink/bot/getconfig → {ret, typing_ticket, ...}."""
    url = f"{_trailing(base_url)}ilink/bot/getconfig"
    body = {
        "ilink_user_id": ilink_user_id,
        "context_token": context_token,
        "base_info": _build_base_info(),
    }
    async with httpx.AsyncClient(timeout=CONFIG_TIMEOUT_S) as c:
        r = await c.post(url, json=body, headers=_build_headers(token))
        r.raise_for_status()
        return r.json()


async def send_typing(
    base_url: str,
    token: str,
    ilink_user_id: str,
    typing_ticket: str,
    status: int = TYPING_STATUS_TYPING,
) -> None:
    """POST ilink/bot/sendtyping to show/cancel 'typing' indicator."""
    url = f"{_trailing(base_url)}ilink/bot/sendtyping"
    body = {
        "ilink_user_id": ilink_user_id,
        "typing_ticket": typing_ticket,
        "status": status,
        "base_info": _build_base_info(),
    }
    async with httpx.AsyncClient(timeout=CONFIG_TIMEOUT_S) as c:
        r = await c.post(url, json=body, headers=_build_headers(token))
        r.raise_for_status()


# ── CDN helpers ──────────────────────────────────────────────────────────


def _build_cdn_download_url(cdn_base_url: str, encrypt_query_param: str) -> str:
    """Construct CDN download URL with properly URL-encoded query param."""
    return f"{cdn_base_url}/download?encrypted_query_param={quote(encrypt_query_param, safe='')}"


async def _fetch_cdn_bytes(
    cdn_base_url: str,
    encrypt_query_param: str,
    full_url: str | None = None,
) -> bytes:
    """Download raw bytes from CDN, trying full_url first then constructed URL as fallback."""
    constructed_url = _build_cdn_download_url(cdn_base_url, encrypt_query_param) if encrypt_query_param else ""

    # Try full_url first (server-returned), fall back to constructed URL.
    urls: list[str] = []
    if full_url:
        urls.append(full_url)
    if constructed_url and constructed_url != full_url:
        urls.append(constructed_url)
    if not urls:
        raise RuntimeError("CDN download: no URL available (neither full_url nor encrypt_query_param)")

    last_err: Exception | None = None
    for url in urls:
        try:
            logger.debug("CDN download: trying url={}", url[:120])
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                })
                if not r.is_success:
                    body_preview = r.text[:200] if r.content else ""
                    logger.warning(
                        "CDN download: HTTP {} for url={} body={}",
                        r.status_code, url[:120], body_preview,
                    )
                    r.raise_for_status()
                return r.content
        except Exception as e:
            last_err = e
            logger.debug("CDN download: failed url={} err={}", url[:120], e)
            if len(urls) > 1:
                continue
    raise last_err or RuntimeError("CDN download: all URLs failed")


async def download_cdn_media(
    cdn_base_url: str,
    encrypt_query_param: str,
    aes_key_hex: str,
    full_url: str | None = None,
) -> bytes:
    """Download and AES-128-ECB decrypt a CDN file.

    Tries *full_url* first (server-returned complete download URL), falls back
    to constructing the URL from *encrypt_query_param* + *cdn_base_url*.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    ciphertext = await _fetch_cdn_bytes(cdn_base_url, encrypt_query_param, full_url)

    decryptor = Cipher(algorithms.AES(bytes.fromhex(aes_key_hex)), modes.ECB()).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # PKCS7 unpad
    pad = plaintext[-1]
    if 1 <= pad <= 16 and all(b == pad for b in plaintext[-pad:]):
        plaintext = plaintext[:-pad]
    return plaintext


async def download_cdn_media_plain(
    cdn_base_url: str,
    encrypt_query_param: str,
    full_url: str | None = None,
) -> bytes:
    """Download raw bytes from CDN without decryption (for unencrypted media)."""
    return await _fetch_cdn_bytes(cdn_base_url, encrypt_query_param, full_url)


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

    with open(file_path, "rb") as f:
        raw_data = f.read()
    rawsize = len(raw_data)
    raw_md5 = hashlib.md5(raw_data).hexdigest()
    aes_key = secrets.token_bytes(16)

    # PKCS7 pad + AES-128-ECB encrypt
    pad_len = 16 - (rawsize % 16)
    padded = raw_data + bytes([pad_len] * pad_len)
    encryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

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
        "base_info": _build_base_info(),
    }
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as c:
        r = await c.post(upload_url, json=upload_body, headers=_build_headers(token))
        r.raise_for_status()
        upload_resp = r.json()

    upload_full_url = (upload_resp.get("upload_full_url") or "").strip()
    upload_param = upload_resp.get("upload_param") or ""
    if not upload_full_url and not upload_param:
        raise RuntimeError(
            f"getuploadurl returned no upload URL (need upload_full_url or upload_param), "
            f"resp={upload_resp}"
        )

    if upload_full_url:
        cdn_url = upload_full_url
    else:
        cdn_url = f"{cdn_base_url}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(filekey)}"

    download_param = ""
    max_retries = 3
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(cdn_url, content=ciphertext, headers={"Content-Type": "application/octet-stream"})
                if 400 <= r.status_code < 500:
                    err_msg = r.headers.get("x-error-message", r.text[:200])
                    raise RuntimeError(f"CDN upload client error {r.status_code}: {err_msg}")
                if r.status_code != 200:
                    err_msg = r.headers.get("x-error-message", f"status {r.status_code}")
                    raise RuntimeError(f"CDN upload server error: {err_msg}")
                download_param = r.headers.get("x-encrypted-param", "")
                if not download_param:
                    raise RuntimeError("CDN response missing x-encrypted-param header")
                break
        except RuntimeError as e:
            last_error = e
            if "client error" in str(e):
                raise
            if attempt < max_retries:
                logger.warning("CDN upload attempt {}/{} failed, retrying: {}", attempt, max_retries, e)
            else:
                logger.error("CDN upload all {} attempts failed: {}", max_retries, e)
    if not download_param:
        raise last_error or RuntimeError(f"CDN upload failed after {max_retries} attempts")

    return {
        "filekey": filekey,
        "aeskey": aes_key.hex(),
        "download_param": download_param,
        "filesize_cipher": len(ciphertext),
        "filesize_raw": rawsize,
    }


