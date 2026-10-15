---
name: yuanbao-xcard
description: Author and send a Yuanbao interactive group card.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [yuanbao, xcard, card, group, a2ui]
    related_skills: [yuanbao]
---

# Yuanbao xCard Skill

Builds a self-contained Yuanbao xCard and sends it into the current group with `yb_xcard_send`. It does not render cards in private chats, and it does not cover ordinary text replies. The gateway delivers the card; a later change uses `yb_xcard_update` on the same `surfaceId`.

## When to Use

Use a card when the user must choose, confirm, or scan a short structured list. Answer in plain text when a card would only repeat the sentence.

The adapter has to be started with `platforms.yuanbao.extra.xcard_enabled: true`. If `yb_xcard_send` reports that xCard is disabled, stop and tell the user; do not keep retrying.

## Prerequisites

- A Yuanbao group session. `yb_xcard_send` and `yb_xcard_update` refuse direct messages.
- Python 3 on `PATH` for the static check below. The script uses only the standard library.

## How to Run

Write the card as JSONL (one JSON object per line) in the working directory, then check it with `terminal`:

```bash
python3 /path/to/yuanbao-xcard/scripts/validate_xcard.py card.jsonl
```

Resolve `/path/to/yuanbao-xcard` from the installed skill directory. Pass the same JSONL text as the `messages` argument of `yb_xcard_send`.

## Quick Reference

| Tool | When |
| --- | --- |
| `yb_xcard_send` | First delivery. `mode` is `once` or `perRevision`. |
| `yb_xcard_update` | Same card, new snapshot, after the client has echoed a message id. |
| `terminal` | Run `scripts/validate_xcard.py` before sending. |

`mode=once` closes the card on the first accepted click. `mode=perRevision` stays open and accepts one click per revision. `audience=requester` is the default; use `anyone` only when every member may click.

## Procedure

1. Define the data, then a flat component list whose root id is `root`.
2. First message is `createSurface` with `catalogId` `https://yuanbao.tencent.com/specification/v1_0/catalogs/xcard/catalog.json` and `version` `v1.0`. Then `updateComponents`, then any `updateDataModel`.
3. Give every button event a context object that includes `resultText` the user can read, for example `{"id":"a","resultText":"已选择 A"}`.
4. Keep the card self-contained. Do not use `WebCard`, `xCardTemplate`, `tracking`, `@hostEnv`, or a function that is not in the official catalog.
5. Run the validator with `terminal`. Fix every reported line before calling the tool.
6. Call `yb_xcard_send` with `messages`, `title`, and `mode`. Set `waitForAction` only when this turn cannot continue without the click.
7. On `ok: true` the card is already on screen. Confirm in one short sentence. Do not restate the card.
8. When the user clicks, continue from `context`. Change the same card with `yb_xcard_update` and the original `surfaceId`.

A minimal component list is a `Card` root, a `Text` child, and a `Button` whose `child` is another `Text`. Child ids must exist in the same component array.

## Pitfalls

- An update sent before the client echoes `card_message_id` fails. Wait for a click, or send the card without planning an immediate update.
- A rejected card may already have been replaced by `fallbackText`. Do not send the card again.
- `code` success with a rate-limit message means the card was dropped. Wait, then answer in text.
- Button `userMessage` can also show up as a chat line. The gateway drops one matching echo. Do not treat that line as a new request.
- Cards live in memory for 30 minutes. After a gateway restart, old cards are unknown.

## Verification

- `terminal` exits 0 on the JSONL.
- `yb_xcard_send` returns `ok: true` and a `surfaceId`.
- A click returns `waitStatus: action_received`, or a new turn whose text contains `yuanbao_xcard_action`.
- `yb_xcard_update` on that `surfaceId` returns `delivery: existing_card_updated` once a message id exists.
