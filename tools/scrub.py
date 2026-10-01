"""Fixture ID-scrub (spec/10-slack-io.md section 8).

Pure, offline, fully unit-tested. Two things live here:

- `scrub(payload, scrub_map)` -- applies every rule of the section 8 table to one
  raw Slack payload (a `history/*.json` message, a `reactions.get` response, a
  `users.list` page, a `conversations.info` response, ...), using one shared,
  deterministic `scrub_map` (real id -> placeholder) that the caller persists
  across every file it scrubs so cross-references stay consistent (a `before`
  and its `after` scrub identically, a user mentioned in one message scrubs to
  the same placeholder everywhere else it appears).
- `no_real_identifiers(payload, vocabulary=None)` -- a verifier: returns a list
  of problems found in an already-scrubbed payload (empty = clean). It is the
  thing `capture_fixtures.py` runs on every scrubbed file before it is written,
  and the thing this module's own tests run against constructed "leftover"
  payloads to prove each leftover class is actually caught.

Nothing here touches the network or `slack_sdk`; both functions take plain
dicts/lists/strings and return new ones.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

# --- persisted map location ---------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "tests" / "fixtures"
# Committed: the placeholder vocabulary only, each placeholder mapped to itself.
SCRUB_MAP_PATH = FIXTURES_DIR / "scrub_map.json"
# Never committed (.gitignore): the real-id -> placeholder half. Committing it
# would make every scrubbed fixture reversible back to the real workspace.
LOCAL_MAP_PATH = FIXTURES_DIR / "scrub_map.local.json"

_NOTE_KEY = "_note"
_VOCAB_NOTE = (
    "The placeholder vocabulary: every placeholder the fixtures use, mapped to "
    "itself. The real-id half of the map lives in scrub_map.local.json, which "
    "is never committed. Written by tools/scrub.py; never edit by hand."
)


def _read_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if k != _NOTE_KEY}


def _write_map(data: dict[str, str], path: Path, note: str | None) -> None:
    out: dict[str, str] = dict(data)
    if note is not None:
        out[_NOTE_KEY] = note
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def load_scrub_map(
    path: Path = SCRUB_MAP_PATH, *, local_path: Path | None = LOCAL_MAP_PATH,
) -> dict[str, str]:
    """The working map: the committed vocabulary merged with the local real-id
    entries (when that file exists), `_note` dropped, plus an identity entry
    for every generated placeholder the fixtures beside `path` already use, so
    a new real id never reuses a placeholder a committed fixture names (spec 10
    section 8: stable per real id, consistent across files)."""
    merged = _read_map(path)
    for placeholder in _placeholders_in_fixtures(path.parent):
        merged.setdefault(placeholder, placeholder)
    if local_path is not None:
        merged.update(_read_map(local_path))
    return merged


_FIXTURE_SUBDIRS = ("history", "refetch", "reactions", "controls")


def _placeholders_in_fixtures(fixtures_dir: Path) -> set[str]:
    """Every generated-scheme placeholder (non-singleton prefix + counter)
    found in the fixture JSON under `fixtures_dir`'s capture subdirectories."""
    pattern = re.compile(
        r"\b(?:"
        + "|".join(
            rf"{re.escape(prefix)}\d{{{width}}}"
            for prefix, width, singleton in _CATEGORY_SCHEME.values()
            if not singleton
        )
        + r")\b"
    )
    found: set[str] = set()
    for sub in _FIXTURE_SUBDIRS:
        d = fixtures_dir / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*.json"):
            found.update(pattern.findall(p.read_text(encoding="utf-8")))
    return found


def save_scrub_map(
    scrub_map: dict[str, str], path: Path = SCRUB_MAP_PATH, *, note: str | None = None,
    local_path: Path | None = LOCAL_MAP_PATH,
) -> None:
    """Split and write the map atomically, keys sorted: every real-id entry
    (key != value) goes to `local_path`, and `path` receives only the
    placeholder vocabulary (each value mapped to itself), so the committed
    file never names a real id. `note` documents the local file."""
    vocabulary = {v: v for v in scrub_map.values()}
    real = {k: v for k, v in scrub_map.items() if k != v}
    _write_map(vocabulary, path, _VOCAB_NOTE)
    if local_path is not None and (real or local_path.exists()):
        _write_map(real, local_path, note)


# --- category placeholder schemes ---------------------------------------------
# Every scheme here is a pure function of "what's already in the map" -- no
# hidden state -- so scrub() is deterministic and safe to call repeatedly with
# a growing, persisted map. Semantic roles that need a SPECIFIC placeholder
# (the bot's own user id -> U0BOT01, the watched channel -> C0MAIN01, the
# officers channel -> C0OFF001, ...) are the CALLER's job: pre-seed
# `scrub_map[real_id] = "U0BOT01"` etc. before calling scrub(). scrub() itself
# only invents a placeholder for a real id nobody has assigned yet, and it
# always does so with the generic scheme for that id's category.

_CATEGORY_SCHEME: dict[str, tuple[str, int, bool]] = {
    # category -> (prefix, zero-padded width, singleton)
    "user": ("U0AAA", 3, False),
    "channel": ("C0CH", 3, False),
    "team": ("T0TEAM", 0, True),
    "bot": ("B0BOT", 0, True),
    "file": ("F0FILE", 3, False),
    "app": ("A0APP", 0, True),
    "usergroup": ("S0TEAM", 2, False),
}


def _looks_like_own_placeholder(real_id: str, category: str) -> bool:
    """True when real_id is already shaped like THIS category's own
    placeholder scheme -- the exact zero-padded-counter shape scrub()
    generates (e.g. "F0FILE039"), or any other `<prefix><alnum>` id (e.g. a
    hand-picked "F0FILEDEL"). Lets scrub() stay a no-op on payloads that are
    already scrubbed even for a category (file ids) the persisted map does
    not enumerate one entry per real id for."""
    prefix, _width, singleton = _CATEGORY_SCHEME[category]
    # Real Slack ids in this workspace are 11 characters; a real id that
    # happens to start with a placeholder prefix (e.g. `C0CH...`) must still
    # get a fresh placeholder, never an identity mapping.
    if len(real_id) >= 11:
        return False
    if singleton:
        return bool(re.match(rf"^{re.escape(prefix)}\d*$", real_id))
    return bool(re.match(rf"^{re.escape(prefix)}[A-Z0-9]+$", real_id))


def _assign(scrub_map: dict[str, str], real_id: Any, category: str) -> Any:
    """Return real_id's placeholder, assigning a new one (and recording it in
    scrub_map) the first time real_id is seen. Non-string ids pass through
    unchanged (e.g. an int attachment id is never a Slack id). An id that
    already looks like this category's own placeholder is recorded as an
    identity mapping and returned unchanged, without consuming a counter slot."""
    if not isinstance(real_id, str) or not real_id:
        return real_id
    if real_id in scrub_map:
        return scrub_map[real_id]
    # Already a placeholder: one this map produces (the seeded role ids
    # U0BOT01 / C0MAIN01 / C0OFF001 sit outside every generated prefix) or
    # one shaped like this category's own scheme.
    if real_id in scrub_map.values() or _looks_like_own_placeholder(real_id, category):
        scrub_map[real_id] = real_id
        return real_id
    prefix, width, singleton = _CATEGORY_SCHEME[category]
    existing = list(scrub_map.values())
    if singleton:
        if prefix not in existing:
            placeholder = prefix
        else:
            n = 2
            while f"{prefix}{n}" in existing:
                n += 1
            placeholder = f"{prefix}{n}"
    else:
        pat = re.compile(rf"^{re.escape(prefix)}(\d{{{width}}})$")
        used = [int(m.group(1)) for v in existing if (m := pat.match(v))]
        n = (max(used) + 1) if used else 1
        placeholder = f"{prefix}{n:0{width}d}"
    scrub_map[real_id] = placeholder
    return placeholder


def _numeric_suffix(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"(\d+)$", s)
    return int(m.group(1)) if m else None


# --- URL scrubbing --------------------------------------------------------------

_FIXTURE_URL_PREFIX = "https://fixture.invalid/"

_THUMB_KEYS = (
    "thumb_1024", "thumb_960", "thumb_720", "thumb_480",
    "thumb_360", "thumb_160", "thumb_80", "thumb_64", "thumb_video",
)

_SCRUBBED_NAME_RE = re.compile(r"^(?:photo|clip)-\d+\.[A-Za-z0-9]+$")
_SCRUBBED_TITLE_RE = re.compile(r"^(?:photo|clip)-\d+$")
_FIXTURE_FILE_URL_RE = re.compile(r"^https://fixture\.invalid/files/[A-Za-z0-9]+/")

# `thumb_tiny` is the uploaded photo itself, inline as base64: a fixture keeps
# the key (the shape is real) but never the bytes.
_THUMB_TINY_PLACEHOLDER = "AAAA"


def _scrub_generic_url(url: str, url_cache: dict[str, str]) -> str:
    """Any URL not already under fixture.invalid gets a deterministic
    fixture.invalid placeholder, stable within one scrub() call (this cache is
    not persisted -- these are incidental URLs, e.g. an unfurled attachment
    link, not identifiers that need cross-file stability)."""
    if url.startswith(_FIXTURE_URL_PREFIX):
        return url
    if url in url_cache:
        return url_cache[url]
    placeholder = f"{_FIXTURE_URL_PREFIX}misc/{len(url_cache) + 1}"
    url_cache[url] = placeholder
    return placeholder


# --- mention / subteam rewriting inside text ------------------------------------

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_SUBTEAM_RE = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|[^>]*)?>")
# Slack wraps links in `text` as `<url>` or `<url|label>`: never eat the `>`/`|`.
_URL_RE = re.compile(r"https?://[^\s<>|]+")


def _scrub_text(text: str, scrub_map: dict[str, str], url_cache: dict[str, str]) -> str:
    def _mention(m: re.Match[str]) -> str:
        return f"<@{_assign(scrub_map, m.group(1), 'user')}>"

    def _subteam(m: re.Match[str]) -> str:
        return f"<!subteam^{_assign(scrub_map, m.group(1), 'usergroup')}>"

    def _url(m: re.Match[str]) -> str:
        return _scrub_generic_url(m.group(0), url_cache)

    text = _MENTION_RE.sub(_mention, text)
    text = _SUBTEAM_RE.sub(_subteam, text)
    text = _URL_RE.sub(_url, text)
    return text


# --- file objects ---------------------------------------------------------------

def _scrub_file(f: dict[str, Any], scrub_map: dict[str, str]) -> dict[str, Any]:
    out = dict(f)
    real_id = out.get("id")
    placeholder = _assign(scrub_map, real_id, "file")
    out["id"] = placeholder

    mimetype = str(out.get("mimetype", ""))
    is_video = mimetype.startswith("video/")
    filetype = out.get("filetype")
    ext = filetype or (mimetype.split("/", 1)[-1] if "/" in mimetype else "bin")
    name_prefix = "clip" if is_video else "photo"
    n = _numeric_suffix(placeholder if isinstance(placeholder, str) else None) or 0

    if "name" in out:
        existing_name = out["name"]
        # Already scrubbed (e.g. a standalone capture -- not part of the
        # sequentially-numbered history/ catalog -- named "photo-1.jpg" by
        # hand): leave it exactly as-is rather than renumber it from the file
        # id, so scrub() stays a no-op on an already-scrubbed fixture.
        if not (isinstance(existing_name, str) and _SCRUBBED_NAME_RE.match(existing_name)):
            out["name"] = f"{name_prefix}-{n}.{ext}"
    # A phone upload's `title` is the device's photo UUID (a real capture
    # showed one): normalised like `name`, stem only, the way Slack sets it.
    existing_title = out.get("title")
    if isinstance(existing_title, str) and not _SCRUBBED_TITLE_RE.match(existing_title):
        out["title"] = f"{name_prefix}-{n}"
    if "thumb_tiny" in out:
        out["thumb_tiny"] = _THUMB_TINY_PLACEHOLDER
    # The uploader and their team are ids like any other. `shares` carries
    # channel names and per-share user lists the parser never reads: dropped.
    if isinstance(out.get("user"), str):
        out["user"] = _assign(scrub_map, out["user"], "user")
    if isinstance(out.get("user_team"), str):
        out["user_team"] = _assign(scrub_map, out["user_team"], "team")
    out.pop("shares", None)
    for key in ("channels", "groups", "ims"):
        if isinstance(out.get(key), list):
            out[key] = [
                _assign(scrub_map, c, "channel") if isinstance(c, str) else c for c in out[key]
            ]

    for key, val in list(out.items()):
        if key in ("permalink", "permalink_public"):
            out[key] = None
        elif isinstance(val, str) and _FIXTURE_FILE_URL_RE.match(val):
            pass  # already one of our own fixture.invalid file urls -- leave as-is
        elif key == "url_private" and isinstance(val, str):
            out[key] = f"{_FIXTURE_URL_PREFIX}files/{placeholder}/download/{name_prefix}.{ext}"
        elif key == "url_private_download" and isinstance(val, str):
            out[key] = (
                f"{_FIXTURE_URL_PREFIX}files/{placeholder}/download/{name_prefix}.{ext}?dl=1"
            )
        elif key in _THUMB_KEYS and isinstance(val, str):
            out[key] = f"{_FIXTURE_URL_PREFIX}files/{placeholder}/{key}.jpg"
        elif isinstance(val, str) and val.startswith(("http://", "https://")):
            out[key] = f"{_FIXTURE_URL_PREFIX}files/{placeholder}/{key}"
        # everything else (mimetype, is_tombstoned, mode, file_access, size,
        # original_w/h, filetype, thumb_*_w/_h, ...) is kept as-is (section 8:
        # these are the fields the live-image test and repost signature need).
    return out


# --- generic dict/list walk ------------------------------------------------------

_TOKEN_KEYS = {"token", "access_token", "bot_token", "user_token", "xoxb_token", "xoxp_token"}
_DROP_KEYS = _TOKEN_KEYS | {"client_msg_id"}

_SINGLE_ID_KEYS = {
    "user": "user",
    "user_id": "user",
    "parent_user_id": "user",
    "creator": "user",
    "context_team_id": "team",
    "bot_id": "bot",
    "app_id": "app",
    "api_app_id": "app",
    "team": "team",
    "team_id": "team",
    "usergroup_id": "usergroup",
    "channel": "channel",
}


def _looks_like_slack_id(s: str) -> bool:
    return bool(re.match(r"^[UWCGTBFAS][A-Z0-9]{1,}$", s))


# A Slack id's own prefix names its category, whatever key it sits under
# (a user-token bot id appears in `edited.user` before `bot_id`).
_PREFIX_CATEGORY = {
    "U": "user", "W": "user", "C": "channel", "G": "channel", "T": "team",
    "B": "bot", "F": "file", "A": "app", "S": "usergroup",
}

_PROFILE_TO_NAME_KEYS = (
    "display_name", "real_name", "display_name_normalized", "real_name_normalized",
    "username", "first_name",
)
_PROFILE_TO_EMPTY_KEYS = (
    "last_name", "title", "phone", "skype", "email", "pronouns",
    "status_text", "status_text_canonical", "status_emoji",
)


def _scrub_profile_names(profile: dict[str, Any], name: str) -> None:
    """Rewrite, in place, every name-bearing and free-text field of a profile
    dict (a users.list `profile` or a message's `user_profile`)."""
    for pk in _PROFILE_TO_NAME_KEYS:
        if pk in profile:
            profile[pk] = name
    for pk in _PROFILE_TO_EMPTY_KEYS:
        if pk in profile:
            profile[pk] = ""
    if "avatar_hash" in profile:
        profile["avatar_hash"] = "0000000000"
    if "fields" in profile:
        profile["fields"] = {}
    if "status_emoji_display_info" in profile:
        profile["status_emoji_display_info"] = []


def _scrub_dict(d: dict[str, Any], scrub_map: dict[str, str], url_cache: dict[str, str]) -> dict[str, Any]:
    is_file_obj = "mimetype" in d and "id" in d
    is_user_entry = "profile" in d and "id" in d
    is_channel_obj = "id" in d and ("is_member" in d or "is_private" in d)
    # `bot_profile` (a message posted through an app's user token carries one):
    # its `id` is a bot id and its `name` is the app's name.
    is_bot_profile = "id" in d and "app_id" in d and "icons" in d
    # An attachment unfurling a Slack message names its author, and its
    # `fallback` line repeats that name ("[date] name: text").
    is_author_attachment = any(
        key in d for key in ("is_msg_unfurl", "author_name", "author_subname")
    )

    out: dict[str, Any] = {}
    for k, v in d.items():
        # A key that is itself a full Slack id (e.g. `pinned_info` keyed by
        # channel id) goes through the same id map as values do.
        if isinstance(k, str) and _REAL_ID_RE.fullmatch(k):
            new_key = _assign(scrub_map, k, _PREFIX_CATEGORY.get(k[0], "user"))
            out[new_key] = _scrub_node(v, scrub_map, url_cache)
            continue
        if k in _DROP_KEYS:
            continue
        if k in ("permalink", "permalink_public"):
            out[k] = None
            continue
        if k == "files" and isinstance(v, list):
            out[k] = [_scrub_file(f, scrub_map) if isinstance(f, dict) else f for f in v]
            continue
        if k in ("users", "reply_users") and isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[k] = [_assign(scrub_map, x, "user") if _looks_like_slack_id(x) else x for x in v]
            continue
        if k in ("shared_team_ids", "internal_team_ids") and isinstance(v, list):
            out[k] = [_assign(scrub_map, x, "team") if isinstance(x, str) else x for x in v]
            continue
        if k == "id" and isinstance(v, str):
            if is_file_obj:
                out[k] = _assign(scrub_map, v, "file")
            elif is_user_entry:
                out[k] = _assign(scrub_map, v, "user")
            elif is_channel_obj:
                out[k] = _assign(scrub_map, v, "channel")
            elif is_bot_profile:
                out[k] = _assign(scrub_map, v, "bot")
            else:
                out[k] = v
            continue
        if k == "name" and is_bot_profile and isinstance(v, str):
            out[k] = "user-bot"
            continue
        if k == "user_profile" and isinstance(v, dict):
            profile = _scrub_node(v, scrub_map, url_cache)
            owner = d.get("user")
            placeholder = None
            if isinstance(owner, str) and owner:
                owner_cat = _PREFIX_CATEGORY.get(owner[0], "user") if _looks_like_slack_id(owner) else "user"
                placeholder = _assign(scrub_map, owner, owner_cat)
            n = _numeric_suffix(placeholder) if isinstance(placeholder, str) else None
            name = f"user-{n}" if n is not None else "user"
            _scrub_profile_names(profile, name)
            if "name" in profile:
                profile["name"] = name
            out[k] = profile
            continue
        if k in ("author_name", "author_subname") and isinstance(v, str):
            out[k] = ""
            continue
        if k == "fallback" and is_author_attachment and isinstance(v, str):
            out[k] = ""
            continue
        if k == "username" and isinstance(v, str):
            if v and (d.get("bot_id") or d.get("subtype") == "bot_message"):
                out[k] = "user-bot"
            else:
                out[k] = ""
            continue
        if k in _SINGLE_ID_KEYS and isinstance(v, str):
            cat = _SINGLE_ID_KEYS[k]
            if _looks_like_slack_id(v):
                cat = _PREFIX_CATEGORY.get(v[0], cat)
            out[k] = _assign(scrub_map, v, cat)
            continue
        if k == "text" and isinstance(v, str):
            out[k] = _scrub_text(v, scrub_map, url_cache)
            continue
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            out[k] = _scrub_generic_url(v, url_cache)
            continue
        out[k] = _scrub_node(v, scrub_map, url_cache)

    if is_user_entry and isinstance(out.get("id"), str):
        n = _numeric_suffix(out["id"])
        name = "user-bot" if d.get("is_bot") else (f"user-{n}" if n is not None else "user")
        out["name"] = name
        # A real users.list entry (G2) also carries the person's name at the
        # top level and split across profile.first_name / last_name, plus an
        # avatar hash derived from their email and free-text profile fields.
        if "real_name" in out:
            out["real_name"] = name
        profile = out.get("profile")
        if isinstance(profile, dict):
            _scrub_profile_names(profile, name)
    return out


def _scrub_node(node: Any, scrub_map: dict[str, str], url_cache: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return _scrub_dict(node, scrub_map, url_cache)
    if isinstance(node, list):
        return [_scrub_node(v, scrub_map, url_cache) for v in node]
    return node


def scrub(payload: Any, scrub_map: dict[str, str]) -> Any:
    """Apply the section 8 scrub rules to one raw Slack payload.

    `scrub_map` is mutated in place: any real id not already present gets a
    freshly assigned placeholder recorded into it (first-seen order), so the
    caller can persist it (save_scrub_map) after each call and reuse it for
    the next file. `payload` itself is never mutated; a new, scrubbed
    structure is returned. Calling scrub() again on payload that already uses
    the map's placeholder vocabulary is a no-op.
    """
    return _scrub_node(payload, scrub_map, {})


# --- verifier --------------------------------------------------------------------

# U/W/C/G/T/B/F/A followed by 8+ more alnum chars: long enough to look like a
# real Slack id (our own placeholders are shorter, except a few that
# coincidentally reach this length -- those are excluded via `vocabulary`).
_REAL_ID_RE = re.compile(r"(?<![A-Za-z0-9])([UWCGTBFA][A-Z0-9]{8,})(?![A-Za-z0-9])")

# The only names a scrubbed users.list entry may carry (section 8: never a real name).
_PLACEHOLDER_NAME_RE = re.compile(r"^(?:user-\d+|user-bot|user)?$")
_USER_NAME_KEYS = ("name", "real_name")
_PROFILE_NAME_KEYS = (
    "real_name", "display_name", "real_name_normalized", "display_name_normalized",
    "first_name", "username",
)


def _default_vocabulary() -> set[str]:
    return set(load_scrub_map().values())


def _matches_a_placeholder_scheme(token: str) -> bool:
    """True when token is already shaped like one of our OWN generated
    placeholders (any category) -- always safe, whether or not this exact
    real id happened to get recorded into a persisted map (file ids are
    the common case: the persisted map only enumerates the small roster of
    named roles, never one entry per photo)."""
    return any(
        _looks_like_own_placeholder(token, category) for category in _CATEGORY_SCHEME
    )


def no_real_identifiers(payload: Any, vocabulary: Iterable[str] | None = None) -> list[str]:
    """Return a list of leftover-identifier problems in an already-scrubbed
    payload (empty list = clean). Catches three classes:

    - a real-looking id (U/W/C/G/T/B/F/A + 8 or more alnum) that is not one of
      the known placeholder values (`vocabulary`, defaulting to the persisted
      scrub_map.json's values);
    - any http(s) URL that is not under https://fixture.invalid/;
    - a token-like key left in the payload, or a permalink*/permalink_public
      key whose value was not scrubbed to null;
    - a users.list entry (a dict with `id` and `profile`) whose name fields
      are anything but the `user-<n>` / `user-bot` placeholders, or whose
      `profile.last_name` is non-empty.
    """
    vocab = set(vocabulary) if vocabulary is not None else _default_vocabulary()
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "profile" in node and "id" in node:
                for k in _USER_NAME_KEYS:
                    v = node.get(k)
                    if isinstance(v, str) and not _PLACEHOLDER_NAME_RE.match(v):
                        problems.append(f"unscrubbed name at {path}.{k}")
                profile = node.get("profile")
                if isinstance(profile, dict):
                    for k in _PROFILE_NAME_KEYS:
                        v = profile.get(k)
                        if isinstance(v, str) and not _PLACEHOLDER_NAME_RE.match(v):
                            problems.append(f"unscrubbed name at {path}.profile.{k}")
                    if profile.get("last_name"):
                        problems.append(f"unscrubbed name at {path}.profile.last_name")
            user_profile = node.get("user_profile")
            if isinstance(user_profile, dict):
                for k in _PROFILE_NAME_KEYS + ("name",):
                    v = user_profile.get(k)
                    if isinstance(v, str) and not _PLACEHOLDER_NAME_RE.match(v):
                        problems.append(f"unscrubbed name at {path}.user_profile.{k}")
                if user_profile.get("last_name"):
                    problems.append(f"unscrubbed name at {path}.user_profile.last_name")
            for k in ("author_name", "author_subname", "username"):
                v = node.get(k)
                if isinstance(v, str) and not _PLACEHOLDER_NAME_RE.match(v):
                    problems.append(f"unscrubbed name at {path}.{k}")
            for k, v in node.items():
                key_l = str(k).lower()
                if key_l in _TOKEN_KEYS or "token" in key_l:
                    problems.append(f"token-like key left at {path}.{k}")
                if k in ("permalink", "permalink_public") and v is not None:
                    problems.append(f"unscrubbed {k} at {path}.{k}: {v!r}")
                # Slack keys some objects by id (a pinned message's
                # `pinned_info` is keyed by channel id): check keys too.
                if isinstance(k, str):
                    walk(k, f"{path}.<key>")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for m in _REAL_ID_RE.finditer(node):
                token = m.group(1)
                if token not in vocab and not _matches_a_placeholder_scheme(token):
                    problems.append(f"real-looking id leftover at {path}: {token}")
            for m in _URL_RE.finditer(node):
                url = m.group(0)
                if not url.startswith(_FIXTURE_URL_PREFIX):
                    problems.append(f"non-fixture URL leftover at {path}: {url}")

    walk(payload, "$")
    return problems
