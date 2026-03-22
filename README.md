# nanobot-channel-weixin

Personal WeChat (微信) channel plugin for [nanobot](https://github.com/HKUDS/nanobot).

个人微信 Channel 插件，让你通过微信(客户端: 设置->插件，灰度中)与 nanobot AI 助手对话。

<img src="grayscale.jpg" alt="WeChat Plugin (灰度中)" width="300">

---

## Disclaimer / 免责声明

**EN:** This plugin is a Python re-implementation based on protocol analysis of the official npm package [`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin). It communicates with the same [iLink Bot API](https://ilinkai.weixin.qq.com) backend but is built entirely from scratch for the nanobot ecosystem — no original source code was copied. This project is for educational, research, and personal use only. It is not affiliated with or endorsed by Tencent or WeChat.

**CN:** 本插件基于对官方 npm 包 [`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin) 的协议分析，使用 Python 从零重新实现。它使用相同的 [iLink Bot API](https://ilinkai.weixin.qq.com) 后端，但完全为 nanobot 生态独立编写，未复制任何原始源码。本项目仅供学习、研究和个人使用，与腾讯或微信官方无关，亦未获其背书。

---

## How It Works / 工作原理

```
WeChat User ──► iLink Bot API (Tencent) ──► nanobot-channel-weixin ──► nanobot Agent
                   (long-poll)                    (Python)                (LLM)
```

- QR code scan to bind your WeChat identity / 扫码绑定微信身份
- Long-poll `getUpdates` to receive messages / 长轮询接收消息
- Reply via `sendMessage` API / 通过 API 回复消息
- Supports text, image, voice, file, and video / 支持文本、图片、语音、文件和视频

---

## Install / 安装

### If nanobot is installed via `uv tool` / 如果你已经通过 uv 安装过 nanobot

```bash
uv pip install git+https://github.com/alvis233/nanobot-channel-weixin.git \
  --python ~/.local/share/uv/tools/nanobot-ai/bin/python
```

Or install together with nanobot: / 或者也可以携带 nanobot-channel-weixin 重装 nanobot

```bash
uv tool install nanobot-ai \
  --with git+https://github.com/alvis233/nanobot-channel-weixin.git
```

### If nanobot is installed via `pip`

```bash
pip install git+https://github.com/alvis233/nanobot-channel-weixin.git
```

### From source / 从源码安装

```bash
git clone https://github.com/alvis233/nanobot-channel-weixin.git
cd nanobot-channel-weixin
pip install -e .
```

---

## Usage / 使用

### 1. Verify plugin is detected / 验证插件已识别

```bash
nanobot plugins list
```

You should see: / 你应该看到：

```
│ WeChat   │ plugin  │ no      │
```

### 2. Login with QR code / 扫码登录

```bash
# If installed via uv tool:
~/.local/share/uv/tools/nanobot-ai/bin/python -m nanobot_channel_weixin login

# If installed via pip:
nanobot-weixin login
```

Scan the QR code with your WeChat app to bind your account. Credentials are saved to `~/.nanobot/state/weixin/`.

用微信扫描终端中的二维码完成绑定，凭证自动保存到 `~/.nanobot/state/weixin/`。

### 3. Configure / 配置

The login command auto-enables the channel in `~/.nanobot/config.json`. You can also configure manually:

登录命令会自动在配置文件中启用频道，也可以手动配置：

```json
{
  "channels": {
    "weixin": {
      "enabled": true,
      "allowFrom": ["*"]
    }
  }
}
```

### 4. Start gateway / 启动网关

```bash
nanobot gateway
```

Now send a message to your WeChat bot — nanobot will reply!

现在通过微信给机器人发消息，nanobot 就会回复！

<img src="example.jpg" alt="Chat Demo" width="400">

---

## Re-login / 重新登录

If your session expires, simply run the login command again:

如果登录态失效，重新执行登录命令即可：

```bash
~/.local/share/uv/tools/nanobot-ai/bin/python -m nanobot_channel_weixin login
```

---

## Security / 安全性

The iLink Bot API has built-in access control:

iLink Bot API 自带多层访问控制：

- Only users who actively message your bot can be seen / 只有主动给你发消息的用户才会出现
- Replies require a server-issued `context_token` — the bot cannot message arbitrary users / 回复需要服务端签发的 `context_token`，机器人无法主动骚扰他人
- `allowFrom: ["*"]` is safe in this context / 在此场景下设置 `allowFrom: ["*"]` 是安全的

---

## Project Structure / 项目结构

```
nanobot-channel-weixin/
├── pyproject.toml                      # Package config & entry points
└── nanobot_channel_weixin/
    ├── __init__.py                     # Exports WeixinChannel
    ├── channel.py                      # WeixinChannel(BaseChannel)
    ├── api.py                          # iLink Bot HTTP API client
    ├── auth.py                         # QR login & credential storage
    ├── cli.py                          # nanobot-weixin CLI
    └── __main__.py                     # python -m support
```

---

## Dependencies / 依赖

- [nanobot-ai](https://github.com/HKUDS/nanobot) — the nanobot framework
- [httpx](https://www.python-httpx.org/) — async HTTP client
- [cryptography](https://cryptography.io/) — AES-128-ECB for CDN media
- [qrcode](https://pypi.org/project/qrcode/) — terminal QR code display

---

## Acknowledgments / 致谢

- [nanobot](https://github.com/HKUDS/nanobot) by HKUDS — the ultra-lightweight AI assistant framework
- [`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin) — the official OpenClaw WeChat plugin whose protocol was referenced

---

## License / 许可证

[MIT](LICENSE)
