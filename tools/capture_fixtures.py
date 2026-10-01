"""G2 capture tool (spec/10-slack-io.md section 8; PLAN.md section 9 L1 / section 13 gate G2).

Replaces the hand-authored provisional fixtures under tests/fixtures/ with
real, scrubbed Slack captures, file-for-file -- same paths, same filenames,
same shapes -- without changing any test (tests/fixtures/PROVISIONAL.md).

NETWORK ONLY RUNS when this is invoked without --dry-run *and* both
SLACK_BOT_TOKEN and SLACK_USER_TOKEN are set in the environment; anything
else (printing the plan, the owner's phone checklist) never touches
slack_sdk. A token is read from the environment and is never logged, printed,
or written to a fixture; every log line carries Slack IDs only. This module
never hardcodes a bot or workspace name -- the watched channel and the
officers/report channel come from the loaded snipebot config, never a
literal channel name or id.

Usage:
    python tools/capture_fixtures.py --dry-run
    python tools/capture_fixtures.py --dry-run --only photo-and-tag
    SLACK_BOT_TOKEN=... SLACK_USER_TOKEN=... python tools/capture_fixtures.py
    SLACK_BOT_TOKEN=... SLACK_USER_TOKEN=... python tools/capture_fixtures.py --only heic
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Run directly (`python tools/capture_fixtures.py`), the repo root is not on
# sys.path -- only tools/ is. Put it there before importing snipebot / tools.*
# so this works both as a script and as `python -m tools.capture_fixtures` /
# an ordinary package import from tests.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snipebot.config import Config, ConfigError, load_config  # noqa: E402
from snipebot.ts import Ts, format_ts, parse_ts  # noqa: E402
from tools.scrub import (  # noqa: E402
    FIXTURES_DIR,
    load_scrub_map,
    no_real_identifiers,
    save_scrub_map,
    scrub,
)
PROVISIONAL_MD = FIXTURES_DIR / "PROVISIONAL.md"
CAPTURED_MD = FIXTURES_DIR / "CAPTURED.md"

_MAP_NOTE = (
    "Deterministic real-id -> placeholder map, built and grown by "
    "tools/scrub.py / tools/capture_fixtures.py across every real capture. "
    "Never edit by hand."
)


# --- the plan (spec/10-slack-io.md section 8; PLAN.md section 5/9 L1) ----------

@dataclass(frozen=True)
class ShapeSpec:
    name: str
    poster: str  # "user_token" | "owner_phone" | "not_reproducible"
    note: str = ""


SHAPES: tuple[ShapeSpec, ...] = (
    ShapeSpec("photo-and-tag", "user_token"),
    ShapeSpec("photo-only", "user_token"),
    ShapeSpec("tag-only", "user_token"),
    ShapeSpec("multi-tag", "user_token"),
    ShapeSpec("multi-image", "user_token"),
    ShapeSpec("thread-reply", "user_token"),
    ShapeSpec("thread-reply-broadcast", "user_token"),
    ShapeSpec("image-link", "user_token"),
    ShapeSpec("mention-in-quote-or-code", "user_token"),
    ShapeSpec(
        "usergroup-mention", "user_token",
        "skipped with a note if the workspace has no user group",
    ),
    ShapeSpec("at-channel", "user_token"),
    ShapeSpec("gif", "user_token"),
    ShapeSpec("video", "user_token"),
    ShapeSpec(
        "slack-connect-file", "not_reproducible",
        "not reproducible on a throwaway workspace; the provisional file stays",
    ),
    ShapeSpec(
        "heic", "owner_phone",
        "owner posts from a phone; located by a marker in the message text",
    ),
    ShapeSpec(
        "ios-multi-photo-share", "owner_phone",
        "owner posts from a phone; located by a marker in the message text",
    ),
)

REFETCH_CASES: tuple[str, ...] = (
    "tag-edited-in", "tag-edited-out", "file-deleted", "message-deleted",
    "reply-added", "veto-by-target", "veto-by-third-party",
)

OTHER_CAPTURES: tuple[str, ...] = (
    "reactions.get (paired with the veto/selfie reaction cases)",
    "users.list page (users/users.json)",
    "conversations.info for the watched channel and the officers channel",
    "digest.json (one real posted digest, run with --no-react, or captured after the rig)",
)

# The non-shape captures a full run always does; each is also reachable alone
# through --only so a partial re-run never re-posts every shape.
EXTRA_ONLY: tuple[str, ...] = ("channels", "users", "digest", "probe")

MARKER_PREFIX = "[G2-capture]"


def owner_marker(shape: str) -> str:
    """The exact text the owner includes in a phone-posted message so this
    tool can find it later by scanning history for the marker string."""
    return f"{MARKER_PREFIX} {shape}"


# --- plan / dry-run -------------------------------------------------------------

def format_plan(only: str | None = None) -> str:
    known_names = {s.name for s in SHAPES} | set(REFETCH_CASES) | set(EXTRA_ONLY)
    if only is not None and only not in known_names:
        return f"unknown shape or case for --only: {only!r}\nknown: {sorted(known_names)}"

    lines: list[str] = [
        "G2 capture plan (spec/10-slack-io.md section 8; PLAN.md section 9 L1 / section 13 G2)",
        "",
        "Shapes this tool posts itself with the user token:",
    ]
    for s in SHAPES:
        if only is not None and s.name != only:
            continue
        if s.poster != "user_token":
            continue
        suffix = f"  -- {s.note}" if s.note else ""
        lines.append(f"  - {s.name}{suffix}")

    lines += ["", "Shapes the owner posts from a phone (this tool then locates them by marker):"]
    for s in SHAPES:
        if only is not None and s.name != only:
            continue
        if s.poster != "owner_phone":
            continue
        lines.append(f"  - {s.name}: post with the text marker {owner_marker(s.name)!r}")

    lines += ["", "Shapes not captured (the hand-authored provisional file stays):"]
    for s in SHAPES:
        if only is not None and s.name != only:
            continue
        if s.poster != "not_reproducible":
            continue
        lines.append(f"  - {s.name}  -- {s.note}")

    lines += ["", "Before/after re-fetch pairs (post, capture before.json, mutate, capture after.json):"]
    for case in REFETCH_CASES:
        if only is not None and case != only:
            continue
        lines.append(f"  - {case}")

    if only is None:
        lines += ["", "Also captured:"]
        for item in OTHER_CAPTURES:
            lines.append(f"  - {item}")

        lines += [
            "",
            "G2 probe: upload one JPEG twice with the user token, fetch both thumb_1024",
            "bodies with the bot token, compare bytes, and print BYTE-IDENTICAL or",
            "DIFFERENT (with sizes). Decides whether parse.rendition_url keeps",
            "thumb_1024 first or moves the repost key to url_private_download (10",
            "section 8); the tool only prints the recommendation, it never edits parse.py.",
            "",
            "Owner phone checklist:",
            "  1. Take a HEIC photo and share it to the watched channel with the text",
            f"     {owner_marker('heic')!r}.",
            "  2. Use the iOS multi-photo share sheet to post several photos at once to",
            f"     the watched channel with the text {owner_marker('ios-multi-photo-share')!r}.",
            "  3. Tell the runner once both are posted; it scans recent history for the",
            "     markers and captures them.",
        ]
    return "\n".join(lines)


# --- fixture I/O ------------------------------------------------------------------

def _write_scrubbed(path: Path, raw_payload: Any, scrub_map: dict[str, str]) -> None:
    """Scrub raw_payload, verify it, and write it to path (creating parent
    dirs). Raises RuntimeError (never writes) if the verifier finds a leftover
    real identifier, URL, or token/permalink -- a capture must never land a
    fixture that failed its own scrub."""
    # A slack_sdk response object is written as the plain dict Slack sent.
    if not isinstance(raw_payload, (dict, list)) and hasattr(raw_payload, "data"):
        raw_payload = raw_payload.data
    scrubbed = scrub(raw_payload, scrub_map)
    problems = no_real_identifiers(scrubbed, vocabulary=scrub_map.values())
    if problems:
        raise RuntimeError(
            f"refusing to write {path}: scrub left leftovers:\n  " + "\n  ".join(problems)
        )
    # Serialize first, then replace: a failure mid-dump must never leave a
    # truncated fixture behind (it did once, at G2).
    text = json.dumps(scrubbed, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _rewrite_provisional_to_captured(sources: dict[str, str]) -> None:
    """PROVISIONAL.md -> CAPTURED.md, listing each file's scrubbed source ts
    and the capture date. `sources` maps a fixture's relative path to a short
    'ts=... captured=...' note."""
    if not PROVISIONAL_MD.exists():
        return
    body = PROVISIONAL_MD.read_text(encoding="utf-8")
    today = _dt.date.today().isoformat()
    # Merge with the sources an earlier (partial) run already recorded, so a
    # series of --only runs accumulates instead of overwriting.
    if CAPTURED_MD.exists():
        for line in CAPTURED_MD.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^- `([^`]+)` -- (.+)$", line)
            if m and m.group(1) not in sources:
                sources[m.group(1)] = m.group(2)
    if not sources:
        return
    header = (
        f"# Captured fixtures (L1 -> real, gate G2, {today})\n\n"
        "These fixtures were captured from a real, throwaway Slack workspace and "
        "scrubbed by tools/scrub.py (spec/10-slack-io.md section 8). They replace "
        "the hand-authored provisional versions file-for-file; no test changed.\n\n"
        "Shapes this tool posted went through the app's user token, so a text-only "
        "message among them carries `bot_id`, `app_id` and `bot_profile` next to its "
        "real `user` (10 section 4 E-G2-1); uploads carry none of those. Phone-posted "
        "shapes are a person's own client output.\n\n"
        "## Source capture ts (scrubbed) by file\n\n"
        + "\n".join(f"- `{path}` -- {note}" for path, note in sorted(sources.items()))
        + "\n\n---\n\n"
    )
    CAPTURED_MD.write_text(header + body, encoding="utf-8")


# --- real network path ------------------------------------------------------------

def _require_tokens() -> tuple[str, str] | None:
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not bot_token or not user_token:
        return None
    return bot_token, user_token


def _build_clients(bot_token: str, user_token: str) -> tuple[Any, Any]:
    """Lazily import slack_sdk so nothing above this call ever touches the
    network -- --dry-run and the no-token refusal path never reach here."""
    from slack_sdk import WebClient

    return WebClient(token=bot_token), WebClient(token=user_token)


def _fetch_message(bot_client: Any, channel: str, ts: Ts) -> dict[str, Any] | None:
    ts_str = format_ts(ts)
    resp = bot_client.conversations_history(
        channel=channel, oldest=ts_str, latest=ts_str, inclusive=True,
        include_all_metadata=True, limit=1,
    )
    messages = resp.get("messages") or []
    return messages[0] if messages else None


def _fetch_message_until(
    bot_client: Any, channel: str, ts: Ts, key: str, attempts: int = 8,
) -> dict[str, Any] | None:
    """Refetch a message until it carries `key` (a link unfurl lands on the
    message asynchronously, seconds after the post) or attempts run out; the
    last fetch is returned either way."""
    raw = None
    for attempt in range(attempts):
        raw = _fetch_message(bot_client, channel, ts)
        if raw is not None and raw.get(key):
            return raw
        time.sleep(1 + attempt)
    return raw


def _fetch_reply(bot_client: Any, channel: str, parent_ts: Ts, ts: Ts) -> dict[str, Any] | None:
    """A non-broadcast thread reply never appears in conversations.history
    (seen on the real API); it is read through conversations.replies on its
    parent, exactly as the fetch path under test does."""
    resp = bot_client.conversations_replies(
        channel=channel, ts=format_ts(parent_ts), include_all_metadata=True, limit=200,
    )
    ts_str = format_ts(ts)
    for msg in resp.get("messages") or []:
        if msg.get("ts") == ts_str:
            return msg
    return None


def _post_text(user_client: Any, channel: str, text: str, **kwargs: Any) -> Ts:
    resp = user_client.chat_postMessage(channel=channel, text=text, **kwargs)
    return parse_ts(str(resp["ts"]))


def _share_ts_of(user_client: Any, file_id: str | None, channel: str) -> str | None:
    """The ts of an uploaded file's share into `channel`, read back from
    files.info. completeUploadExternal registers the share asynchronously, so
    the upload response itself carries no ts (seen on the real API); poll
    briefly. Keyed on the file id, so a message somebody else posts in the
    channel meanwhile can never be mistaken for this upload."""
    if not file_id:
        return None
    for attempt in range(6):
        shares = (user_client.files_info(file=file_id).get("file") or {}).get("shares") or {}
        for visibility in ("public", "private"):
            for entry in (shares.get(visibility) or {}).get(channel) or []:
                if entry.get("ts"):
                    return str(entry["ts"])
        time.sleep(1 + attempt)
    return None


def _post_photo(
    user_client: Any, channel: str, text: str, photo_paths: list[Path], **kwargs: Any
) -> Ts:
    resp = user_client.files_upload_v2(
        channel=channel, initial_comment=text,
        file_uploads=[{"file": str(p), "filename": p.name} for p in photo_paths],
        **kwargs,
    )
    files_resp = resp.get("files") or [resp.get("file", {})]
    first = files_resp[0] or {}
    ts_val = first.get("ts") or first.get("shares_ts") or resp.get("ts")
    if not ts_val:
        ts_val = _share_ts_of(user_client, first.get("id"), channel)
    if not ts_val:
        raise RuntimeError("upload succeeded but Slack echoed no share ts (files.info shares empty)")
    return parse_ts(str(ts_val))


def _sample_photo() -> Path:
    # The 1280 px image: Slack only renders thumbs up to the original size, so
    # a sub-1024 px upload gets no thumb_1024 at all (seen at G2) and would
    # misrepresent a phone photo's rendition set.
    return FIXTURES_DIR / "photos" / "landscape_no_face.jpg"


# A direct link to a real, public-domain image (the landscape photo listed in
# tests/fixtures/photos/SOURCES.md), so Slack's media unfurl attaches an
# `image_url`. A made-up URL never unfurls (seen at G2).
_IMAGE_LINK_URL = (
    "https://commons.wikimedia.org/wiki/Special:FilePath/"
    "Field,_corn,_Liechtenstein,_Mountains,_Alps,_Vaduz,_sky,_clouds,_landscape.jpg?width=1024"
)

_SHAPE_NO_ASSET_NOTE = (
    "no bundled sample asset for this shape yet -- add one under "
    "tools/assets/ before running a real capture"
)


def capture_shape(
    bot_client: Any, user_client: Any, channel: str, shape: ShapeSpec,
    target_user: str, second_target: str, third_target: str,
    scrub_map: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Post (or locate) one L1 shape and write its scrubbed fixture. Returns
    (captured, note).

    `scrub_map` is the run's one map (spec 10 section 8): every assignment made
    here lands in it, and the caller saves it, so two real uploads never share
    a placeholder across shapes or runs. Called alone, the map is loaded and
    saved here."""
    dest = FIXTURES_DIR / "history" / f"{shape.name}.json"
    owns_map = scrub_map is None
    if scrub_map is None:
        scrub_map = load_scrub_map()

    def _write(raw_payload: Any) -> None:
        _write_scrubbed(dest, raw_payload, scrub_map)
        if owns_map:
            save_scrub_map(scrub_map, note=_MAP_NOTE)

    if shape.poster == "not_reproducible":
        return False, shape.note

    if shape.poster == "owner_phone":
        marker = owner_marker(shape.name)
        history = bot_client.conversations_history(channel=channel, limit=50)
        for msg in history.get("messages") or []:
            if marker in str(msg.get("text", "")):
                _write(msg)
                return True, "located by marker"
        return False, f"marker {marker!r} not found yet -- ask the owner to post it"

    if shape.name == "usergroup-mention":
        try:
            groups = (bot_client.usergroups_list().get("usergroups") or [])
        except Exception as exc:  # missing usergroups:read, or a plan without user groups
            return False, f"usergroups.list unavailable ({type(exc).__name__}); provisional file stays"
        if not groups:
            return False, "no user group exists in this workspace"
        usergroup_id = groups[0]["id"]
        text = f"<!subteam^{usergroup_id}> got <@{target_user}>"
        ts = _post_text(user_client, channel, text)
        raw = _fetch_message(bot_client, channel, ts)
        if raw is None:
            return False, "posted but not found on refetch"
        _write(raw)
        return True, f"captured ts={format_ts(ts)}"

    text_by_shape: dict[str, str] = {
        "photo-and-tag": f"got <@{target_user}>",
        "photo-only": "nice",
        "tag-only": f"got <@{target_user}>",
        "multi-tag": f"got <@{target_user}> <@{second_target}> <@{third_target}> <@{target_user}>",
        "multi-image": f"got <@{target_user}>",
        "thread-reply": f"got <@{target_user}>",
        "thread-reply-broadcast": f"got <@{target_user}>",
        "image-link": f"look {_IMAGE_LINK_URL} <@{target_user}>",
        # PROVISIONAL.md roles: the quoted mention (the target) is kept; the
        # inline-code and fenced mentions are dropped.
        "mention-in-quote-or-code": (
            f"> quoting <@{target_user}>\n`code <@{second_target}>`\n```fenced <@{third_target}>```"
        ),
        "at-channel": f"<!channel> game on <@{target_user}>",
        "gif": f"got <@{target_user}>",
        "video": f"clip <@{target_user}>",
    }
    text = text_by_shape.get(shape.name, "")
    parent_ts: Ts | None = None

    # Every shape the FX table (50 section 2.1) expects to be COUNTED carries a
    # photo; only tag-only is deliberately fileless.
    if shape.name in (
        "photo-and-tag", "photo-only", "multi-image", "tag-only",
        "multi-tag", "at-channel", "mention-in-quote-or-code",
    ):
        photos = [_sample_photo()] if shape.name != "multi-image" else [_sample_photo(), _sample_photo()]
        if shape.name == "tag-only":
            ts = _post_text(user_client, channel, text)
        else:
            ts = _post_photo(user_client, channel, text, photos)
    elif shape.name in ("gif", "video"):
        return False, _SHAPE_NO_ASSET_NOTE
    elif shape.name == "thread-reply":
        parent_ts = _post_text(user_client, channel, "parent")
        ts = _post_text(user_client, channel, text, thread_ts=format_ts(parent_ts))
    elif shape.name == "thread-reply-broadcast":
        parent_ts = _post_text(user_client, channel, "parent")
        ts = _post_text(
            user_client, channel, text,
            thread_ts=format_ts(parent_ts), reply_broadcast=True,
        )
    else:
        ts = _post_text(user_client, channel, text)

    if shape.name == "thread-reply" and parent_ts is not None:
        raw = _fetch_reply(bot_client, channel, parent_ts, ts)
    elif shape.name == "image-link":
        raw = _fetch_message_until(bot_client, channel, ts, key="attachments")
        if raw is not None and not raw.get("attachments"):
            return False, "posted, but Slack attached no unfurl within the wait"
    else:
        raw = _fetch_message(bot_client, channel, ts)
    if raw is None:
        return False, "posted but not found on refetch"
    _write(raw)
    return True, f"captured ts={format_ts(ts)}"


def capture_refetch_case(
    bot_client: Any, user_client: Any, channel: str, case: str,
    target_user: str, third_party_user: str,
) -> tuple[bool, str]:
    """Post, capture before.json, perform the case's mutation, capture
    after.json (spec/10-slack-io.md section 8 'Before/after re-fetch pairs').

    Both veto cases react with the bot token as a stand-in for the real
    reactor: this tool only ever holds one non-bot (user) token, so it cannot
    truly react "as" a second, distinct workspace member. The capture still
    exercises the real reaction shape; it is not a true veto-by-that-specific-
    person capture, and that limitation is worth knowing before trusting the
    `users[]` id inside veto-by-target.json / veto-by-third-party.json."""
    dest_dir = FIXTURES_DIR / "refetch" / case
    scrub_map = load_scrub_map()
    after: dict[str, Any] | None

    if case in ("tag-edited-in", "tag-edited-out"):
        # PROVISIONAL.md roles: `in` goes from one tag (the target) to two (a
        # second member edited in); `out` goes from those two back to one.
        one_tag = f"got <@{target_user}>"
        two_tags = f"got <@{target_user}> <@{third_party_user}>"
        starts_with_two = case == "tag-edited-out"
        ts = _post_text(user_client, channel, two_tags if starts_with_two else one_tag)
        before = _fetch_message(bot_client, channel, ts)
        new_text = one_tag if starts_with_two else two_tags
        user_client.chat_update(channel=channel, ts=format_ts(ts), text=new_text)
        after = _fetch_message(bot_client, channel, ts)
    elif case == "file-deleted":
        ts = _post_photo(user_client, channel, f"got <@{target_user}>", [_sample_photo()])
        before = _fetch_message(bot_client, channel, ts)
        file_id = ((before or {}).get("files") or [{}])[0].get("id")
        if file_id:
            user_client.files_delete(file=file_id)
        after = _fetch_message(bot_client, channel, ts)
    elif case == "message-deleted":
        ts = _post_text(user_client, channel, f"got <@{target_user}>")
        before = _fetch_message(bot_client, channel, ts)
        user_client.chat_delete(channel=channel, ts=format_ts(ts))
        after = {"messages": []}
    elif case == "reply-added":
        ts = _post_text(user_client, channel, f"got <@{target_user}>")
        before = _fetch_message(bot_client, channel, ts)
        _post_text(user_client, channel, "reply", thread_ts=format_ts(ts))
        after = _fetch_message(bot_client, channel, ts)
    elif case in ("veto-by-target", "veto-by-third-party"):
        ts = _post_photo(user_client, channel, f"got <@{target_user}>", [_sample_photo()])
        before = _fetch_message(bot_client, channel, ts)
        bot_client.reactions_add(channel=channel, timestamp=format_ts(ts), name="x")
        after = _fetch_message(bot_client, channel, ts)
        # reactions.get is complete/untruncated (unlike history's reactions array),
        # which the before/after history refetch alone does not exercise.
        reactions_payload = bot_client.reactions_get(channel=channel, timestamp=format_ts(ts), full=True)
        _write_scrubbed(FIXTURES_DIR / "reactions" / f"{case}.json", reactions_payload, scrub_map)
    else:
        return False, f"unknown refetch case: {case}"

    if before is None:
        return False, "posted but not found on refetch (before)"
    if after is None:
        return False, "posted but not found on refetch (after)"
    _write_scrubbed(dest_dir / "before.json", before, scrub_map)
    _write_scrubbed(dest_dir / "after.json", after, scrub_map)
    save_scrub_map(scrub_map, note=_MAP_NOTE)
    return True, f"captured ts={format_ts(ts)}"


def capture_users_list(bot_client: Any) -> None:
    scrub_map = load_scrub_map()
    data = bot_client.users_list(limit=200)
    _write_scrubbed(FIXTURES_DIR / "users" / "users.json", data, scrub_map)
    save_scrub_map(scrub_map, note=_MAP_NOTE)


def capture_channels(bot_client: Any, main_channel: str, officers_channel: str | None) -> None:
    scrub_map = load_scrub_map()
    # Same guard as _seed_roles: a placeholder already held by another real id
    # is never reused; scrub() then generates a fresh one for this channel.
    _pin_roles(scrub_map, [(main_channel, "C0MAIN01"), (officers_channel or "", "C0OFF001")])
    _write_scrubbed(
        FIXTURES_DIR / "channels" / "main.json",
        bot_client.conversations_info(channel=main_channel), scrub_map,
    )
    if officers_channel:
        _write_scrubbed(
            FIXTURES_DIR / "channels" / "officers.json",
            bot_client.conversations_info(channel=officers_channel), scrub_map,
        )
    save_scrub_map(scrub_map, note=_MAP_NOTE)


# The name spans report._row_line prints in a digest's standings: a pair row
# ("N. <a> → <b> — <count>"), any other row ("N. <name> — <number> ...") and
# a "(top: <name>)" suffix. Display names are never mentions, so scrub()
# cannot reach them. Each name span is greedy and anchored to the fixed
# numeric tail, so a name that itself holds " — " is replaced whole.
_DIGEST_PAIR_ROW = re.compile(r"^(\d+\. )(.+?)( → )(.+)( — \d+$)")
_DIGEST_ROW = re.compile(r"^(\d+\. )(.+)( — \d)")
_DIGEST_TOP = re.compile(r"\(top: (.+)\)$")


def _scrub_digest_names(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a digest message with every printed display name
    replaced by a stable `user-<n>` label (spec 10 section 8: never a real
    name in a committed fixture). Metadata and block structure are kept."""
    labels: dict[str, str] = {}

    def label(name: str) -> str:
        return labels.setdefault(name, f"user-{len(labels) + 1}")

    def line(text: str) -> str:
        m = _DIGEST_PAIR_ROW.match(text)
        if m:
            text = (m.group(1) + label(m.group(2)) + m.group(3) + label(m.group(4))
                    + m.group(5) + text[m.end():])
        else:
            m = _DIGEST_ROW.match(text)
            if m:
                text = m.group(1) + label(m.group(2)) + m.group(3) + text[m.end():]
        return _DIGEST_TOP.sub(lambda t: f"(top: {label(t.group(1))})", text)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: ("\n".join(line(part) for part in v.split("\n"))
                    if k == "text" and isinstance(v, str) else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    out = dict(msg)
    if isinstance(out.get("text"), str):
        out["text"] = "\n".join(line(part) for part in out["text"].split("\n"))
    if "blocks" in out:
        out["blocks"] = walk(out["blocks"])
    return out


def capture_digest(bot_client: Any, channel: str) -> tuple[bool, str]:
    """Locate a real posted digest (a bot_message with metadata.event_type ==
    "snipe_digest") by scanning recent history; this tool does not itself run
    a sync -- run one with --no-react (or capture after the rig) first."""
    scrub_map = load_scrub_map()
    history = bot_client.conversations_history(channel=channel, limit=50)
    for msg in history.get("messages") or []:
        metadata = msg.get("metadata") or {}
        if metadata.get("event_type") == "snipe_digest":
            _write_scrubbed(
                FIXTURES_DIR / "history" / "digest.json", _scrub_digest_names(msg), scrub_map,
            )
            save_scrub_map(scrub_map, note=_MAP_NOTE)
            return True, "captured"
    return False, "no snipe_digest message found yet -- run a sync with --no-react first"


def run_g2_probe(bot_client: Any, user_client: Any, bot_token: str, channel: str) -> str:
    """Upload the same JPEG twice via the user token, fetch both thumb_1024
    bodies with the bot token, and report whether they are byte-identical
    (spec/10-slack-io.md section 8, 'G2 probe'). Never edits parse.py --
    prints the recommendation only."""
    import urllib.request

    sample = _sample_photo()
    tss = [
        _post_photo(user_client, channel, "g2-probe", [sample])
        for _ in range(2)
    ]
    bodies: list[bytes] = []
    sizes: list[int] = []
    for ts in tss:
        raw = _fetch_message(bot_client, channel, ts)
        if raw is None or not raw.get("files"):
            return "G2 probe inconclusive: could not refetch an uploaded probe file"
        url = raw["files"][0].get("thumb_1024")
        if not url:
            return "G2 probe inconclusive: no thumb_1024 on the probe upload"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
        bodies.append(body)
        sizes.append(len(body))

    identical = len(bodies) == 2 and bodies[0] == bodies[1]
    verdict = "BYTE-IDENTICAL" if identical else "DIFFERENT"
    recommendation = (
        "keep parse.rendition_url's thumb_1024-first order"
        if identical
        else "move the repost key to url_private_download (one-line change in parse.py)"
    )
    return f"G2 probe: {verdict} (sizes={sizes}) -- recommendation: {recommendation}"


def _seed_roles(
    scrub_map: dict[str, str], *, sniper: str, targets: tuple[str, ...], bot_user: str,
    bot_id: str, team: str, main_channel: str, officers_channel: str | None,
) -> None:
    """Pin the fixture roles (tests/fixtures/PROVISIONAL.md, "Roles are stable
    across every file") onto the real ids before anything is scrubbed, so a
    real capture lands on the placeholders the provisional fixtures -- and
    every test reading them -- already use. Identity entries (the committed
    vocabulary and the placeholders committed fixtures already use) are kept,
    so a later generated placeholder never collides with one of them."""
    pairs: list[tuple[str, str]] = [
        (sniper, "U0AAA001"), (bot_user, "U0BOT01"), (bot_id, "B0BOT"),
        (team, "T0TEAM"), (main_channel, "C0MAIN01"),
    ]
    distinct_targets = list(dict.fromkeys(t for t in targets if t))
    pairs += list(zip(distinct_targets, ("U0AAA002", "U0AAA003", "U0AAA004")))
    if officers_channel:
        pairs.append((officers_channel, "C0OFF001"))
    _pin_roles(scrub_map, pairs)


def _pin_roles(scrub_map: dict[str, str], pairs: list[tuple[str, str]]) -> None:
    """Map each unmapped real id to its role placeholder unless another real
    id already holds that placeholder. Only a real id (non-identity entry)
    holds a placeholder; the committed vocabulary's identity entries (spec 10
    section 8, E-G2-2) never do."""
    taken = {v for k, v in scrub_map.items() if k != v}
    for real_id, placeholder in pairs:
        if real_id and real_id not in scrub_map and placeholder not in taken:
            scrub_map[real_id] = placeholder
            taken.add(placeholder)


def run_capture(bot_token: str, user_token: str, config: Config, only: str | None) -> int:
    bot_client, user_client = _build_clients(bot_token, user_token)
    channel = config.channel
    officers_channel = next((r.post_to for r in config.reports if r.post_to is not None), None)
    sources: dict[str, str] = {}

    admins = list(config.admins)
    target_user = admins[0] if admins else ""
    second_target = admins[1] if len(admins) > 1 else target_user
    third_target = admins[2] if len(admins) > 2 else target_user

    bot_auth = bot_client.auth_test()
    user_auth = user_client.auth_test()
    scrub_map = load_scrub_map()
    _seed_roles(
        scrub_map,
        sniper=str(user_auth.get("user_id") or ""),
        targets=(target_user, second_target, third_target),
        bot_user=str(bot_auth.get("user_id") or ""),
        bot_id=str(bot_auth.get("bot_id") or ""),
        team=str(bot_auth.get("team_id") or ""),
        main_channel=channel, officers_channel=officers_channel,
    )
    save_scrub_map(scrub_map, note=_MAP_NOTE)

    for shape in SHAPES:
        if only is not None and shape.name != only:
            continue
        captured, note = capture_shape(
            bot_client, user_client, channel, shape, target_user, second_target, third_target,
            scrub_map=scrub_map,
        )
        print(f"{'captured' if captured else 'skipped'}: {shape.name} -- {note}")
        if captured:
            sources[f"history/{shape.name}.json"] = note
        save_scrub_map(scrub_map, note=_MAP_NOTE)

    for case in REFETCH_CASES:
        if only is not None and case != only:
            continue
        captured, note = capture_refetch_case(
            bot_client, user_client, channel, case, target_user, second_target
        )
        print(f"{'captured' if captured else 'skipped'}: refetch/{case} -- {note}")
        if captured:
            sources[f"refetch/{case}/before.json"] = note
            sources[f"refetch/{case}/after.json"] = note
            if case.startswith("veto-by-"):
                sources[f"reactions/{case}.json"] = f"{note} (reactions.get, full)"

    if only in (None, "channels"):
        capture_channels(bot_client, channel, officers_channel)
        sources["channels/main.json"] = "captured (conversations.info)"
        if officers_channel:
            sources["channels/officers.json"] = "captured (conversations.info)"
        print("captured: channels/")
    if only in (None, "users"):
        capture_users_list(bot_client)
        sources["users/users.json"] = "captured (users.list)"
        print("captured: users/users.json")
    if only in (None, "digest"):
        captured, note = capture_digest(bot_client, channel)
        print(f"{'captured' if captured else 'skipped'}: digest.json -- {note}")
        if captured:
            sources["history/digest.json"] = note
    if only in (None, "probe"):
        probe_result = run_g2_probe(bot_client, user_client, bot_token, channel)
        print(probe_result)

    _rewrite_provisional_to_captured(sources)
    return 0


# --- CLI --------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="capture_fixtures", description="G2 real-Slack fixture capture tool.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the plan and the owner's phone checklist; touches no network",
    )
    parser.add_argument(
        "--only", default=None, metavar="SHAPE",
        help="restrict capture (or the printed plan) to one shape or refetch case",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="config.yaml path (source of the watched channel and roster; default config.yaml)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.dry_run:
        print(format_plan(args.only))
        return 0

    tokens = _require_tokens()
    if tokens is None:
        print(
            "refusing: SLACK_BOT_TOKEN and SLACK_USER_TOKEN must both be set "
            "(no network without both tokens)",
            file=sys.stderr,
        )
        return 1
    bot_token, user_token = tokens

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"refusing: config not found: {config_path}", file=sys.stderr)
        return 1
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"refusing: config invalid: {exc}", file=sys.stderr)
        return 1

    return run_capture(bot_token, user_token, config, args.only)


if __name__ == "__main__":
    raise SystemExit(main())
