# Yuanbao

Connect Hermes to **Yuanbao** (腾讯元宝) — Tencent's AI assistant platform — supporting private (C2C) and group ("派/Pai") messages with image, file, and sticker support.

## Overview

The Yuanbao adapter uses the [Yuanbao Bot Open Platform](https://bot.yuanbao.tencent.com) to:

- Maintain a persistent **WebSocket** connection to the Yuanbao gateway
- Authenticate via **HMAC-signed token** (App ID + App Secret)
- Receive and send text, image, file, and sticker messages
- Support both **C2C (direct)** and **Group (派/Pai)** conversations
- Show typing indicators via the **Reply Heartbeat** protocol (RUNNING / FINISH)
- Query group info and member lists via built-in tools

## Prerequisites

1. **Yuanbao Bot Application** — Register at the [Yuanbao Bot Open Platform](https://bot.yuanbao.tencent.com):
   - Create a new bot application and note your **App ID** and **App Secret**
   - The **Bot ID** is returned automatically by the sign-token API during authentication

2. **Dependencies** — The adapter requires `websockets` and `httpx`:
   ```bash
   pip install websockets httpx
   ```

## Configuration

### Interactive setup

```bash
hermes setup gateway
```

Select **Yuanbao** from the platform list and follow the prompts.

### Manual configuration

Set the required environment variables in `~/.hermes/.env`:

```bash
YUANBAO_APP_ID=your-app-id
YUANBAO_APP_SECRET=your-app-secret
```

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `YUANBAO_APP_ID` | Yuanbao Bot App ID (required) | — |
| `YUANBAO_APP_SECRET` | Yuanbao Bot App Secret (required) | — |
| `YUANBAO_BOT_ID` | Bot account ID (optional, auto-fetched from sign-token API) | — |
| `YUANBAO_WS_URL` | WebSocket gateway URL | `wss://bot-wss.yuanbao.tencent.com/wss/connection` |
| `YUANBAO_API_DOMAIN` | REST API domain | `https://bot.yuanbao.tencent.com` |
| `YUANBAO_ROUTE_ENV` | Internal routing environment (test/staging/production) | — |
| `YUANBAO_HOME_CHANNEL` | Chat ID for cron/notification delivery | — |
| `YUANBAO_HOME_CHANNEL_NAME` | Display name for home channel | `Home` |
| `YUANBAO_ALLOWED_USERS` | Comma-separated user IDs for access control | — |
| `YUANBAO_ALLOW_ALL_USERS` | Set to `true` to allow all users | `false` |
| `YUANBAO_DM_POLICY` | DM access policy: `open`, `allowlist`, or `disabled` | `open` |
| `YUANBAO_DM_ALLOW_FROM` | Comma-separated user IDs for DM allowlist | — |
| `YUANBAO_GROUP_POLICY` | Group access policy: `open`, `allowlist`, or `disabled` | `open` |
| `YUANBAO_GROUP_ALLOW_FROM` | Comma-separated group codes for group allowlist | — |

## Advanced Configuration

For fine-grained control, add platform settings to `~/.hermes/config.yaml`:

```yaml
platforms:
  yuanbao:
    enabled: true
    yuanbao_app_id: "your-app-id"
    yuanbao_app_secret: "your-app-secret"
    yuanbao_ws_url: "wss://bot-wss.yuanbao.tencent.com/wss/connection"
    yuanbao_api_domain: "https://bot.yuanbao.tencent.com"
    yuanbao_dm_policy: "open"            # open | allowlist | disabled
    yuanbao_dm_allow_from: "user1,user2"
    yuanbao_group_policy: "open"         # open | allowlist | disabled
    yuanbao_group_allow_from: "group1,group2"
```

## Media Support

### Images

The adapter supports sending and receiving images. Outbound images are uploaded to **Tencent COS** (Cloud Object Storage) using temporary credentials obtained from the Yuanbao API:

1. Request temporary COS credentials via `genUploadInfo`
2. Upload the image to COS with HMAC-SHA1 signed authorization
3. Send the COS URL as a `TIMImageElem` message

Supported image formats: JPEG, PNG, GIF, BMP, WebP, HEIC, TIFF.

### Files

File attachments (documents, archives, etc.) follow the same COS upload flow and are sent as `TIMFileElem` messages. Maximum file size: **50 MB**.

### Stickers

The adapter supports sending Yuanbao stickers (`TIMFaceElem`) from the built-in sticker catalogue. Stickers can be sent by name or as a random selection.

## Typing Indicator (Reply Heartbeat)

Yuanbao uses a custom **Reply Heartbeat** protocol instead of a standard typing indicator:

- **RUNNING** — sent every 2 seconds while the agent is processing, showing a "typing" animation in the Yuanbao client
- **FINISH** — sent after the final message is delivered, clearing the typing animation

The heartbeat automatically stops after 30 seconds of inactivity as a safety measure.

## Platform-Specific Tools

The `hermes-yuanbao` toolset provides additional tools when running on the Yuanbao platform:

| Tool | Description |
|------|-------------|
| `yb_query_group_info` | Query basic group info (name, owner, member count) |
| `yb_query_group_members` | Search members by name, list bots, or list all members |

These tools are essential for **@mentioning** users in group chats — the agent must query the member list to get the exact nickname before constructing an @mention.

:::tip @Mention Format
To @mention a user in Yuanbao, use the format: `space + @ + nickname + space` (e.g., ` @Alice `). The agent automatically queries group members before mentioning anyone.
:::

## Auto-Sethome

When no home channel is configured, the Yuanbao adapter automatically designates the **first conversation** as the home channel. If the initial home is a group chat and a DM arrives later, the home channel is upgraded to the DM (direct messages take priority over groups).

## Troubleshooting

### Bot fails to connect

This usually means:
- **Invalid App ID / Secret** — Double-check your credentials at the Yuanbao Bot Open Platform
- **Network issues** — Ensure connectivity to `bot-wss.yuanbao.tencent.com` (WebSocket) and `bot.yuanbao.tencent.com` (REST API)
- **Permanent close codes** — Close codes 4012, 4013, 4014, 4018, 4019, 4021 indicate permanent errors that will not trigger reconnection

### Authentication errors

- **Codes 4001, 4002, 4003** — Permanent auth failure. Verify your App ID and App Secret are correct
- **Codes 4010, 4011, 4099** — Transient errors. The adapter will automatically retry with the same token

### Messages not delivered

- Verify the bot is properly connected: check gateway logs for `AUTH_BIND` success
- Check `YUANBAO_DM_POLICY` / `YUANBAO_GROUP_POLICY` if access is restricted
- For group messages, ensure the bot is added to the group (派/Pai)
- Check `YUANBAO_HOME_CHANNEL` for cron/notification delivery

### Image/file upload failures

- Ensure the file size is under **50 MB**
- Check gateway logs for COS credential or upload errors
- Verify network connectivity to Tencent COS endpoints

### Connection drops and reconnection

The adapter automatically reconnects with exponential backoff (up to 100 attempts). Common causes:
- **Heartbeat timeout** — 2 consecutive missed pongs trigger reconnection
- **Network instability** — transient WebSocket disconnections are handled automatically
- **Kickout** — another instance of the same bot connected (only one connection per bot is allowed)
