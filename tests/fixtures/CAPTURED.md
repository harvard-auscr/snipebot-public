# Captured fixtures (L1 -> real, gate G2, 2026-09-23)

These fixtures were captured from a real, throwaway Slack workspace and scrubbed by tools/scrub.py (spec/10-slack-io.md section 8). They replace the hand-authored provisional versions file-for-file; no test changed.

Shapes this tool posted went through the app's user token, so a text-only message among them carries `bot_id`, `app_id` and `bot_profile` next to its real `user` (10 section 4 E-G2-1); uploads carry none of those. Phone-posted shapes are a person's own client output.

## Source capture ts (scrubbed) by file

- `history/at-channel.json` -- captured ts=1790201418.832849
- `history/image-link.json` -- captured ts=1790200884.478039
- `history/multi-image.json` -- captured ts=1790200485.371299
- `history/photo-and-tag.json` -- captured ts=1790200479.312219
- `history/photo-only.json` -- captured ts=1790200482.209139
- `history/tag-only.json` -- captured ts=1790200886.793589
- `history/thread-reply-broadcast.json` -- captured ts=1790200371.430559
- `history/thread-reply.json` -- captured ts=1790200370.091399
- `refetch/file-deleted/after.json` -- captured ts=1790200488.381199
- `refetch/file-deleted/before.json` -- captured ts=1790200488.381199
- `refetch/message-deleted/after.json` -- captured ts=1790200383.070599
- `refetch/message-deleted/before.json` -- captured ts=1790200383.070599
- `refetch/reply-added/after.json` -- captured ts=1790200384.548909
- `refetch/reply-added/before.json` -- captured ts=1790200384.548909
- `refetch/tag-edited-in/after.json` -- captured ts=1790201456.935209
- `refetch/tag-edited-in/before.json` -- captured ts=1790201456.935209
- `refetch/tag-edited-out/after.json` -- captured ts=1790201458.540619
- `refetch/tag-edited-out/before.json` -- captured ts=1790201458.540619
- `refetch/veto-by-target/after.json` -- captured ts=1790200491.677399
- `refetch/veto-by-target/before.json` -- captured ts=1790200491.677399
- `refetch/veto-by-third-party/after.json` -- captured ts=1790200495.356059
- `refetch/veto-by-third-party/before.json` -- captured ts=1790200495.356059

---

# Provisional fixtures (L1)

These fixtures are **hand-authored** from Slack's documented Web API payload shapes
(`conversations.history`, `reactions.get`, `users.list`, `conversations.info`), following the
naming and id-scrub rules in `spec/10-slack-io.md` section 8. They stand in until gate G2,
when scrubbed real captures **replace them file-for-file** — same paths, same filenames, same
shapes — **without changing any test**. Until then they are the inputs the parser and the fake
are tested against, so each one aims to be a faithful, complete Slack payload for the shape it
is named for: one raw message object per `history/` file, every field the parser reads present.

Identifiers are the placeholder vocabulary in `scrub_map.json` (`U0AAA###`, `C0MAIN01`/`C0OFF001`,
`T0TEAM`, `B0BOT`, `F0FILE###`, `A0APP`). All URLs point at `https://fixture.invalid/...`,
every `permalink`/`permalink_public` is `null`, names are `photo-<n>.<ext>`, and no real
names, tokens, `client_msg_id`s or workspace subdomains appear. `ts`/`thread_ts`/`edited.ts`
are opaque strings in September 2026, strictly increasing across the `history/` files.

Roles are stable across every file:

- `U0AAA001` — the sniper (sender of the candidate posts)
- `U0AAA002` / `U0AAA003` / `U0AAA004` — targets
- `U0AAA009` — an admin (eligible veto / selfie actor)
- `U0AAA010` — a non-target, non-admin third party
- `U0AAA011` — a deactivated member
- `U0AAA099` — an off-roster sender (controls only)
- `U0BOT01` / `B0BOT` — the bot user id / bot id
- `A0APP` — the app id; `S0TEAM01` — a user group; `T0TEAM` — the team

Veto emoji name is `x` (owner mapping 2026-09-21); the selfie emoji name is `selfie` (🤳).

## history/ — one raw message per shape

| File | Shape it exhibits | G2 must confirm |
|---|---|---|
| `photo-and-tag.json` | 1 live image + 1 target → COUNTED | thumb/rendition key set present on a real upload |
| `photo-only.json` | 1 live image, no `<@…>` → UNTAGGED | a real caption carries no stray `<@…>` |
| `tag-only.json` | no `files`, 1 target → NO_LIVE_IMAGE | — |
| `multi-tag.json` | 3 targets in mention order, one repeated → deduped len 3 | Slack repeats a duplicate `<@…>` verbatim in `text` and `blocks` |
| `multi-image.json` | 2 live images, 1 target → one COUNTED | multiple uploads arrive under one message's `files` |
| `ios-multi-photo-share.json` | **authored as ONE message with N (=3) files**, 1 target | **G2 decides the real shape:** one message × N files **or** N separate messages. If G2 shows N messages, this file is replaced by N files and `L1-FX-ios-multi-photo-share` reads them; the test asserts "the candidate(s) G2 shows" either way (50 section 2.1) |
| `thread-reply.json` | `thread_ts != ts` (a parent's ts), no `subtype` → NOT_TOP_LEVEL | a non-broadcast reply is visible to the fetch path under test (real channel `history` hides it; captured via replies fetch) |
| `thread-reply-broadcast.json` | `subtype == "thread_broadcast"`, `thread_ts != ts` → NOT_TOP_LEVEL | broadcast reply's `subtype`/`thread_ts` shape |
| `image-link.json` | `attachments[].image_url` present, no uploaded `files` → linked_images>0, live_images==0 | an unfurled image link exposes `image_url` on the attachment |
| `mention-in-quote-or-code.json` | quoted `<@U0AAA002>` kept; inline-code `<@U0AAA003>` and fenced `<@U0AAA004>` dropped | Slack emits quoted mentions as `user` elements under `rich_text_quote`, and code mentions as `text` (not `user`) under code/preformatted — so `text` and `blocks` agree |
| `usergroup-mention.json` | `<!subteam^S0TEAM01>` never a target; real `<@U0AAA002>` still counted | user-group encoding `<!subteam^S…>` / `usergroup` block element |
| `at-channel.json` | `<!channel>` never a target; real `<@U0AAA002>` still counted | broadcast encoding `<!channel>` / `broadcast` block element |
| `heic.json` | `mimetype == image/heic` → live image | iPhone HEIC uploads report `image/heic` |
| `gif.json` | `mimetype == image/gif` → live image | — |
| `video.json` | `mimetype == video/mp4` → live_videos>0, live_images==0 | a video upload's `mimetype`/`thumb_video` shape |
| `slack-connect-file.json` | `file_access == "check_file_info"` → not a live image | Slack Connect shared files stub `file_access` |
| `digest.json` | a `bot_message` (bot_id `B0BOT`, app_id `A0APP`) carrying `metadata.event_type == "snipe_digest"` and the 00 section 9 `event_payload` | `include_all_metadata=true` round-trips `metadata` verbatim under `message.metadata` |

## refetch/<case>/ — before.json + after.json (same `ts`)

| Case | before → after |
|---|---|
| `tag-edited-in` | target `<@U0AAA002>` added by edit; `after` carries `edited.ts > ts` |
| `tag-edited-out` | target `<@U0AAA002>` removed by edit; `after` carries `edited.ts > ts` |
| `file-deleted` | same file id; `after` file `is_tombstoned: true`, message kept |
| `message-deleted` | `after.json = {"messages": []}` — the message's **absence** from its range |
| `reply-added` | `after` gains `thread_ts == ts`, `reply_count`, `latest_reply` |
| `veto-by-target` | `after` gains a `x` reaction by the target `U0AAA002` |
| `veto-by-third-party` | `after` gains a `x` reaction by third party `U0AAA010` |

G2 must confirm the real `edited` block shape (`{user, ts}`), the tombstone flag name
(`is_tombstoned`), and that a deleted message truly vanishes from the range fetch.

## reactions/ — raw `reactions.get` responses (`{ok, type:"message", message{…reactions}}`)

| File | Reaction |
|---|---|
| `veto-by-admin.json` | `x` by admin `U0AAA009` |
| `veto-by-target.json` | `x` by target `U0AAA002` |
| `selfie-by-admin.json` | `selfie` by admin `U0AAA009` |
| `selfie-by-nonadmin.json` | `selfie` by non-admin `U0AAA010` |
| `none.json` | no `reactions` key |
| `truncated-users.json` | `x` with `count: 3` but `users` len 1 (settle-with-refetch case) |

G2 must confirm `reactions.get` truly returns complete `users` (`count == len(users)`); the
`truncated-users` fixture models a `history` truncation, not a real `reactions.get` response.

## users/users.json — a raw `users.list` page

`members[]` with `id`, `name`, `deleted`, `is_bot`, `is_admin`, `profile.display_name` /
`profile.real_name` (all `user-<n>`); one deactivated (`U0AAA011`), one bot carrying `B0BOT`
under `profile.bot_id`; `response_metadata.next_cursor == ""` (single page). G2 confirms
which of `id`/`deleted`/`is_bot`/`profile.*` a real page carries.

## channels/ — raw `conversations.info` responses

`main.json` (`C0MAIN01`, `is_member:true`, `is_private:false`) and `officers.json` (`C0OFF001`,
`is_member:true`, `is_private:true`). G2 confirms the `is_member`/`is_private`/`name` keys.

## controls/ — the paired twins (50 section 2.1)

| File | Purpose |
|---|---|
| `veto-by-target__control.json` | photo-and-tag with **no** reaction → COUNTED under every `veto.by` (control for `L1-RF-veto-by-target`) |
| `tag-edited-in__within-grace/` | before + after where `edited.ts ≤ ts + grace` → COUNTED (control for the "late tag" twin) |
| `photo-and-tag__off-roster-sender.json` | sender `U0AAA099` off the roster → SENDER_OFF_ROSTER |
| `text-blocks-disagree.json` | the **one constructed payload** (50 section 2.1): a photo-and-tag whose `blocks` drop a `<@U0AAA003>` still present in `text`, so `targets == text_mentions`, a `ParseAnomaly` is emitted, and the run is not aborted |

`text-blocks-disagree.json` is deliberately not a faithful natural capture — Slack's `text`
and `blocks` always agree, so this disagreement is mutated in by hand to exercise the anomaly
path. Every other fixture here is a natural shape; only this one stays constructed after G2.

## scrub_map.json

The placeholder vocabulary: each placeholder maps to itself. It stays that way after G2: the
real-id -> placeholder half of the map lives in the gitignored `scrub_map.local.json` on the
capturing machine and is never committed (10 section 8).
