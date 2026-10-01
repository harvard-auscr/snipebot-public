"""In-memory fake of the SlackIO protocol.

Written from Slack's documented Web API payload shapes and the L1 fixtures
(spec 10-slack-io.md section 5), not from snipebot/slack_io.py -- workstream
isolation (plan section 12). FakeSlack satisfies SlackIO structurally so code
under test calls it exactly as it calls the real client.

Source of truth is an append-only event log of authored instants (post, edit,
delete, react, ...). Every read (history, reactions_get, ...) is computed by
folding the events whose `at` is visible `as_of` the simulated clock -- this is
what makes as_of, vanish and reappearance fall out of one replay instead of
mutable per-message state (10 section 7).
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snipebot.slack_io import (
    AlreadyReacted,
    AuthError,
    AuthIdentity,
    ChannelNotFound,
    FileTooLarge,
    HISTORY_PAGE_SIZE,
    MAX_PAGINATION_ITERATIONS,
    MAX_RATE_LIMIT_RETRIES,
    MessageNotFound,
    MissingScope,
    NoReaction,
    NotInChannel,
    RateLimited,
    RawChannel,
    RawMessage,
    RawUser,
    SlackAPIError,
    SlackHTTPError,
    SlackPaginationError,
    SlackTransportError,
    SlackTs,
)
from snipebot.ts import US_PER_SECOND, format_ts, parse_ts

_SECONDS_PER_DAY = 86_400

# File keys checked, in the same preference order as fetch_file_bytes' real
# rendition selector (10 section 2), so a fake-served URL always matches a key
# a test could plausibly have picked.
_FILE_URL_KEYS = ("thumb_1024", "thumb_960", "thumb_720", "thumb_480", "url_private_download")

# Slack error string -> the exception class it maps to (spec section 3 table).
_ERROR_CLASSES: dict[str, type[SlackAPIError]] = {
    "already_reacted": AlreadyReacted,
    "no_reaction": NoReaction,
    "message_not_found": MessageNotFound,
    "not_in_channel": NotInChannel,
    "channel_not_found": ChannelNotFound,
    "missing_scope": MissingScope,
    "not_allowed_token_type": MissingScope,
    "not_authed": AuthError,
    "invalid_auth": AuthError,
    "token_revoked": AuthError,
    "account_inactive": AuthError,
}


def _mk_api_error(error: str) -> SlackAPIError:
    cls = _ERROR_CLASSES.get(error, SlackAPIError)
    err = cls(error)
    err.error = error
    return err


def _mk_rate_limited(retry_after_seconds: int, method: str) -> RateLimited:
    err = RateLimited(retry_after_seconds, method)
    err.retry_after_seconds = retry_after_seconds
    err.method = method
    return err


def _mk_http_error(status: int) -> SlackHTTPError:
    err = SlackHTTPError(status)
    err.status = status
    return err


def _mk_file_too_large(limit_bytes: int) -> FileTooLarge:
    err = FileTooLarge(limit_bytes)
    err.limit_bytes = limit_bytes
    return err


@dataclass
class FakeUser:
    id: str
    is_bot: bool = False
    deleted: bool = False
    display_name: str = ""
    real_name: str = ""


def _copy_files(files: Sequence[dict] | None) -> list[dict]:
    if not files:
        return []
    return [dict(f) for f in files]


def _deleted_file_stub(f: dict, poster: str) -> dict:
    # The real shape of a deleted file (refetch/file-deleted/after.json): Slack
    # swaps the file object for this stub, with no mimetype, name, size,
    # renditions or URLs (spec 10 section 8).
    return {"id": f["id"], "file_access": "file_not_found", "created": 0,
            "timestamp": 0, "user": f.get("user", poster),
            "filetype": f.get("filetype", "")}


def _encode_file(f: dict) -> dict:
    out = dict(f)
    raw = out.pop("_bytes", None)
    if raw is not None:
        out["_bytes_b64"] = base64.b64encode(raw).decode("ascii")
    return out


def _decode_file(f: dict) -> dict:
    out = dict(f)
    packed = out.pop("_bytes_b64", None)
    if packed is not None:
        out["_bytes"] = base64.b64decode(packed.encode("ascii"))
    return out


def _encode_event(e: dict) -> dict:
    out = dict(e)
    data = dict(e["data"])
    files = data.get("files")
    if files:
        data["files"] = [_encode_file(f) for f in files]
    out["data"] = data
    return out


def _decode_event(e: dict) -> dict:
    out = dict(e)
    data = dict(e["data"])
    files = data.get("files")
    if files:
        data["files"] = [_decode_file(f) for f in files]
    out["data"] = data
    return out


class Faults:
    """Reached as fake.faults. Arms a fault on the owning FakeSlack; the fake
    consumes it (by `times`, or never, for a standing fault) as calls match."""

    def __init__(self, slack: "FakeSlack") -> None:
        self._slack = slack

    def rate_limit(self, *, method: str, retry_after_seconds: int, times: int = 1) -> None:
        self._slack._arm("rate_limit", times=times, method=method,
                          retry_after_seconds=retry_after_seconds)

    def fail_mid_page(self, *, after_pages: int = 1, error: str = "internal_error",
                       times: int = 1) -> None:
        self._slack._arm("fail_mid_page", times=times, after_pages=after_pages, error=error)

    def repeat_cursor(self, *, times: int = 1) -> None:
        self._slack._arm("repeat_cursor", times=times)

    def empty_page_with_more(self, *, times: int = 1) -> None:
        self._slack._arm("empty_page_with_more", times=times)

    def vanish(self, *, ts: SlackTs, for_fetches: int = 1) -> None:
        self._slack._arm("vanish", times=for_fetches, ts=ts)

    def lose_post_response(self, *, method: str = "post_message", times: int = 1) -> None:
        self._slack._arm("lose_post_response", times=times, method=method)

    def reaction_error(self, *, method: str, error: str, times: int = 1) -> None:
        self._slack._arm("reaction_error", times=times, method=method, error=error)

    def truncate_reaction_users(self, *, limit: int, drop_bot: bool = False) -> None:
        self._slack._arm("truncate_reaction_users", times=None, replace=True,
                          limit=limit, drop_bot=drop_bot)

    def set_horizon(self, *, days: int | None) -> None:
        self._slack._horizon_days = days

    def fetch_timeout(self, *, times: int = 1) -> None:
        self._slack._arm("fetch_timeout", times=times)

    def fetch_429(self, *, retry_after_seconds: int, times: int = 1) -> None:
        self._slack._arm("fetch_429", times=times, retry_after_seconds=retry_after_seconds)

    def fetch_oversize(self, *, times: int = 1, limit_bytes: int = 0) -> None:
        self._slack._arm("fetch_oversize", times=times, limit_bytes=limit_bytes)

    def fetch_truncate(self, *, times: int = 1) -> None:
        self._slack._arm("fetch_truncate", times=times)


class FakeSlack:  # satisfies SlackIO
    def __init__(
        self,
        *,
        now: SlackTs,
        bot_user_id: str = "U0BOT",
        users: Mapping[str, FakeUser] | None = None,
        channels: Sequence[str] = ("C0MAIN01",),
        bot_member_of: Sequence[str] = ("C0MAIN01",),
        channel_members: Mapping[str, Sequence[str]] | None = None,
        channel_meta: Mapping[str, Mapping[str, Any]] | None = None,
        horizon_days: int | None = 90,
        reaction_users_limit: int | None = None,
    ) -> None:
        self._now = now
        self.bot_user_id = bot_user_id
        self.bot_id = "B0BOT"
        self.app_id = "A0APP"
        self.team_id = "T0TEAM"
        if users is not None:
            self.users: dict[str, FakeUser] = dict(users)
        else:
            self.users = {bot_user_id: FakeUser(id=bot_user_id, is_bot=True)}
        self.channels: list[str] = list(channels)
        self.bot_member_of: list[str] = list(bot_member_of)
        self.channel_members: dict[str, list[str]] | None = (
            {c: list(m) for c, m in channel_members.items()}
            if channel_members is not None else None
        )
        # Per-channel is_private/name, so a reloaded world answers identically
        # (10 sections 5, 7). Missing keys default to False/"".
        self.channel_meta: dict[str, dict[str, Any]] = {
            c: {"is_private": bool((channel_meta or {}).get(c, {}).get("is_private", False)),
                "name": str((channel_meta or {}).get(c, {}).get("name", ""))}
            for c in self.channels
        }
        self._horizon_days = horizon_days
        self._reaction_users_limit = reaction_users_limit
        self._events: list[dict[str, Any]] = []
        self._faults: list[dict[str, Any]] = []
        self.faults = Faults(self)

    # -- fault bookkeeping ---------------------------------------------------

    def _arm(self, name: str, *, times: int | None, replace: bool = False, **fields: Any) -> None:
        if replace:
            self._faults = [f for f in self._faults if f["name"] != name]
        entry: dict[str, Any] = {"name": name, "times": times}
        entry.update(fields)
        self._faults.append(entry)

    def _find_fault(self, name: str, **match: Any) -> dict[str, Any] | None:
        for f in self._faults:
            if f["name"] != name:
                continue
            if all(f.get(k) == v for k, v in match.items()):
                return f
        return None

    def _consume(self, fault: dict[str, Any]) -> None:
        if fault["times"] is None:
            return  # standing fault (e.g. truncate_reaction_users): never auto-disarms
        fault["times"] -= 1
        if fault["times"] <= 0:
            self._faults.remove(fault)

    def _apply_rate_limit_or_raise(self, method: str) -> None:
        # DECISION (writer, fake-slack): FakeSlack takes no injected sleep/clock
        # (constructor has none), so `rate_limit` is resolved for the whole
        # calling method in one shot rather than by literally sleeping and
        # retrying: `times` <= MAX_RATE_LIMIT_RETRIES means the retries are
        # absorbed and the call proceeds to succeed; `times` above that means
        # retries would have been exhausted, so RateLimited is raised now.
        # This matches "exercises the retry path with zero real waiting" (10
        # section 5) without needing a real HTTP retry loop inside the fake.
        fault = self._find_fault("rate_limit", method=method)
        if fault is None:
            return
        times = fault["times"]
        retry_after = fault["retry_after_seconds"]
        self._faults.remove(fault)
        if times > MAX_RATE_LIMIT_RETRIES:
            raise _mk_rate_limited(retry_after, method)

    # -- visibility helpers ---------------------------------------------------

    def _within_horizon(self, ts: SlackTs) -> bool:
        if self._horizon_days is None:
            return True
        now_us = parse_ts(self._now)
        cutoff_us = now_us - self._horizon_days * _SECONDS_PER_DAY * US_PER_SECOND
        return parse_ts(ts) >= cutoff_us

    def _require_channel(self, channel: str, *, require_member: bool) -> None:
        if channel not in self.channels:
            raise _mk_api_error("channel_not_found")
        if require_member and channel not in self.bot_member_of:
            raise _mk_api_error("not_in_channel")

    def _replay(self, *, as_of_now: bool = True,
                deletes_as_of_now: bool = False) -> dict[tuple[str, str], dict[str, Any]]:
        if as_of_now:
            now_us = parse_ts(self._now)
            visible = [e for e in self._events if parse_ts(e["at"]) <= now_us]
        elif deletes_as_of_now:
            now_us = parse_ts(self._now)
            visible = [e for e in self._events
                       if not (e["kind"] in ("delete_message", "delete_file", "edit")
                               and parse_ts(e["at"]) > now_us)]
        else:
            visible = list(self._events)
        visible.sort(key=lambda e: (parse_ts(e["at"]), e["seq"]))

        records: dict[tuple[str, str], dict[str, Any]] = {}
        for e in visible:
            kind = e["kind"]
            channel = e["channel"]
            ts = e["ts"]
            data = e["data"]
            key = (channel, ts)

            if kind == "post":
                records[key] = {
                    "channel": channel, "ts": ts, "user": e["actor"],
                    "text": data.get("text", ""), "files": _copy_files(data.get("files")),
                    "blocks": data.get("blocks"), "attachments": data.get("attachments"),
                    "metadata": data.get("metadata"), "subtype": data.get("subtype"),
                    "bot_message": bool(data.get("bot_message")),
                    "deleted": False, "edited": None,
                    "is_reply": False, "broadcast": False, "thread_ts": None,
                    "reply_count": 0, "reply_users": [], "latest_reply": None,
                    "reactions": {},
                }
            elif kind == "reply":
                parent_ts = data["parent_ts"]
                broadcast = bool(data.get("broadcast"))
                records[key] = {
                    "channel": channel, "ts": ts, "user": e["actor"],
                    "text": data.get("text", ""), "files": _copy_files(data.get("files")),
                    "blocks": None, "attachments": None, "metadata": None,
                    "subtype": "thread_broadcast" if broadcast else None,
                    "bot_message": False,
                    "deleted": False, "edited": None,
                    "is_reply": True, "broadcast": broadcast, "thread_ts": parent_ts,
                    "reply_count": 0, "reply_users": [], "latest_reply": None,
                    "reactions": {},
                }
                parent = records.get((channel, parent_ts))
                if parent is not None:
                    parent["thread_ts"] = parent_ts
                    parent["reply_count"] += 1
                    if e["actor"] not in parent["reply_users"]:
                        parent["reply_users"].append(e["actor"])
                    if (parent["latest_reply"] is None
                            or parse_ts(ts) > parse_ts(parent["latest_reply"])):
                        parent["latest_reply"] = ts
            elif kind == "edit":
                rec = records.get(key)
                if rec is None:
                    continue
                if data.get("text") is not None:
                    rec["text"] = data["text"]
                if data.get("files") is not None:
                    rec["files"] = _copy_files(data["files"])
                if data.get("blocks") is not None:
                    rec["blocks"] = data["blocks"]
                if data.get("metadata") is not None:
                    rec["metadata"] = data["metadata"]
                # Real Slack reports `edited.ts` with a zero fraction (G2 fact 21).
                rec["edited"] = {"user": e["actor"],
                                 "ts": e["at"].split(".", 1)[0] + ".000000"}
            elif kind == "delete_message":
                rec = records.get(key)
                if rec is not None:
                    rec["deleted"] = True
            elif kind == "delete_file":
                rec = records.get(key)
                if rec is not None and rec["files"]:
                    idx = data["file_index"]
                    if 0 <= idx < len(rec["files"]):
                        rec["files"][idx] = _deleted_file_stub(rec["files"][idx], rec["user"])
            elif kind == "react":
                rec = records.get(key)
                if rec is not None:
                    bucket = rec["reactions"].setdefault(data["name"], [])
                    if e["actor"] not in bucket:
                        bucket.append(e["actor"])
            elif kind == "unreact":
                rec = records.get(key)
                if rec is not None:
                    bucket = rec["reactions"].get(data["name"])
                    if bucket and e["actor"] in bucket:
                        bucket.remove(e["actor"])
                        if not bucket:
                            del rec["reactions"][data["name"]]
        return records

    def _get_message(self, channel: str, ts: SlackTs, *, enforce_horizon: bool) -> dict[str, Any]:
        rec = self._replay().get((channel, ts))
        if rec is None or rec["deleted"]:
            raise _mk_api_error("message_not_found")
        if enforce_horizon and not self._within_horizon(ts):
            raise _mk_api_error("message_not_found")
        return rec

    def _effective_truncation(self) -> tuple[int, bool] | None:
        fault = self._find_fault("truncate_reaction_users")
        if fault is not None:
            return (fault["limit"], fault["drop_bot"])
        if self._reaction_users_limit is not None:
            return (self._reaction_users_limit, False)
        return None

    def _render_reactions(self, reactions: dict[str, list[str]], *, complete: bool) -> list[dict]:
        trunc = None if complete else self._effective_truncation()
        out = []
        for name in reactions:
            users = reactions[name]
            count = len(users)
            if trunc is not None:
                limit, drop_bot = trunc
                shown = list(users[:limit])
                if drop_bot and self.bot_user_id in shown:
                    shown.remove(self.bot_user_id)
            else:
                shown = list(users)
            out.append({"name": name, "users": shown, "count": count})
        return out

    def _render_message(self, rec: dict[str, Any], *, complete_reactions: bool) -> RawMessage:
        out: RawMessage = {
            "type": "message", "user": rec["user"], "text": rec["text"],
            "ts": rec["ts"], "team": self.team_id,
        }
        if rec["subtype"]:
            out["subtype"] = rec["subtype"]
        if rec["bot_message"]:
            out["bot_id"] = self.bot_id
            out["app_id"] = self.app_id
        if rec["blocks"] is not None:
            out["blocks"] = rec["blocks"]
        if rec["files"]:
            out["files"] = [{k: v for k, v in f.items() if k != "_bytes"} for f in rec["files"]]
        if rec["attachments"]:
            out["attachments"] = rec["attachments"]
        if rec["metadata"] is not None:
            out["metadata"] = rec["metadata"]
        if rec["thread_ts"] is not None:
            out["thread_ts"] = rec["thread_ts"]
        if rec["edited"] is not None:
            out["edited"] = rec["edited"]
        if rec["reply_count"]:
            out["reply_count"] = rec["reply_count"]
            out["reply_users_count"] = len(rec["reply_users"])
            out["reply_users"] = list(rec["reply_users"])
            out["latest_reply"] = rec["latest_reply"]
        reactions = self._render_reactions(rec["reactions"], complete=complete_reactions)
        if reactions:
            out["reactions"] = reactions
        return out

    def _history_candidates(self, channel: str, oldest: SlackTs,
                             latest: SlackTs | None
                             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Returns (kept records, armed vanish faults that hid a record). The
        # caller consumes the vanish faults only once the fetch completes
        # (10 section 6: a vanish counts COMPLETE fetches only).
        oldest_us = parse_ts(oldest)
        latest_us = parse_ts(latest) if latest is not None else parse_ts(self._now)
        records = self._replay()
        out = []
        for (ch, ts), rec in records.items():
            if ch != channel or rec["deleted"]:
                continue
            if rec["is_reply"] and not rec["broadcast"]:
                continue  # non-broadcast replies are invisible to channel history
            ts_us = parse_ts(ts)
            if not (oldest_us <= ts_us <= latest_us):
                continue
            if not self._within_horizon(ts):
                continue
            out.append(rec)
        out.sort(key=lambda r: parse_ts(r["ts"]), reverse=True)

        kept = []
        vanish_faults: list[dict[str, Any]] = []
        for rec in out:
            vanish_fault = self._find_fault("vanish", ts=rec["ts"])
            if vanish_fault is not None:
                vanish_faults.append(vanish_fault)
                continue
            kept.append(rec)
        return kept, vanish_faults

    def _find_file_bytes(self, url: str) -> bytes | None:
        # Uses the replayed (folded) records rather than raw events so a
        # deleted file (delete_file's URL-less stub) or a removed message (delete_message)
        # is invisible here exactly as it is everywhere else (10 section 7).
        # as_of_now=False: fetch_file_bytes does not gate posts on the simulated
        # clock (unlike history/reactions_get), but deletes_as_of_now=True keeps
        # every future delete_message/delete_file/edit invisible until `now`
        # reaches it: real Slack serves the rendition until the delete (or the
        # edit that swaps the file out) actually happens.
        for rec in self._replay(as_of_now=False, deletes_as_of_now=True).values():
            if rec["deleted"]:
                continue
            files = rec.get("files")
            if not files:
                continue
            for f in files:
                if f.get("is_tombstoned") or f.get("file_access") == "file_not_found":
                    continue
                raw = f.get("_bytes")
                if raw is None:
                    continue
                for key in _FILE_URL_KEYS:
                    if f.get(key) == url:
                        return raw
        return None

    def _mint_ts(self) -> SlackTs:
        used = {e["ts"] for e in self._events if e["kind"] in ("post", "reply")}
        base_us = parse_ts(self._now)
        bump = 0
        while True:
            candidate = format_ts(base_us + bump)
            if candidate not in used:
                return candidate
            bump += 1

    def _append_event(self, kind: str, *, channel: str, ts: SlackTs, actor: str | None,
                       at: SlackTs, data: dict[str, Any]) -> None:
        self._events.append({
            "seq": len(self._events), "at": at, "kind": kind, "channel": channel,
            "ts": ts, "actor": actor, "data": data,
        })

    # -- SlackIO protocol ------------------------------------------------------

    def auth_identity(self) -> AuthIdentity:
        return AuthIdentity(user_id=self.bot_user_id, bot_id=self.bot_id,
                             team_id=self.team_id, url="https://fake.slack.example")

    def history(self, channel: str, oldest: SlackTs, latest: SlackTs | None = None) -> list[RawMessage]:
        self._require_channel(channel, require_member=True)
        self._apply_rate_limit_or_raise("history")

        candidates, vanish_faults = self._history_candidates(channel, oldest, latest)
        fail_fault = self._find_fault("fail_mid_page")
        repeat_fault = self._find_fault("repeat_cursor")
        empty_fault = self._find_fault("empty_page_with_more")

        out: list[RawMessage] = []
        offset = 0
        seen_cursors: set[int] = set()
        page_no = 0
        while True:
            page_no += 1
            if page_no > MAX_PAGINATION_ITERATIONS:
                raise SlackPaginationError("pagination loop guard exceeded")

            if fail_fault is not None and page_no > fail_fault["after_pages"]:
                self._consume(fail_fault)
                if fail_fault["error"] == "transport":
                    raise SlackTransportError("fail_mid_page")
                raise _mk_api_error(fail_fault["error"])

            if empty_fault is not None and page_no == 1:
                self._consume(empty_fault)
                next_offset = offset
                has_more = True
            else:
                chunk = candidates[offset: offset + HISTORY_PAGE_SIZE]
                out.extend(self._render_message(m, complete_reactions=False) for m in chunk)
                next_offset = offset + len(chunk)
                has_more = next_offset < len(candidates)

            if not has_more:
                break

            if repeat_fault is not None and page_no == 2:
                self._consume(repeat_fault)
                next_offset = offset

            if next_offset in seen_cursors:
                raise SlackPaginationError("repeated cursor")
            seen_cursors.add(next_offset)
            offset = next_offset

        for f in vanish_faults:
            self._consume(f)
        return out

    def reactions_get(self, channel: str, ts: SlackTs) -> RawMessage:
        self._require_channel(channel, require_member=True)
        self._apply_rate_limit_or_raise("reactions_get")
        rec = self._get_message(channel, ts, enforce_horizon=True)
        return self._render_message(rec, complete_reactions=True)

    def reactions_add(self, channel: str, ts: SlackTs, name: str) -> None:
        self._require_channel(channel, require_member=True)
        self._apply_rate_limit_or_raise("reactions_add")
        rec = self._get_message(channel, ts, enforce_horizon=True)

        fault = self._find_fault("reaction_error", method="reactions_add")
        if fault is not None:
            self._consume(fault)
            raise _mk_api_error(fault["error"])

        if self.bot_user_id in rec["reactions"].get(name, []):
            raise _mk_api_error("already_reacted")
        self._append_event("react", channel=channel, ts=ts, actor=self.bot_user_id,
                            at=self._now, data={"name": name})

    def reactions_remove(self, channel: str, ts: SlackTs, name: str) -> None:
        self._require_channel(channel, require_member=True)
        self._apply_rate_limit_or_raise("reactions_remove")
        rec = self._get_message(channel, ts, enforce_horizon=True)

        fault = self._find_fault("reaction_error", method="reactions_remove")
        if fault is not None:
            self._consume(fault)
            raise _mk_api_error(fault["error"])

        if self.bot_user_id not in rec["reactions"].get(name, []):
            raise _mk_api_error("no_reaction")
        self._append_event("unreact", channel=channel, ts=ts, actor=self.bot_user_id,
                            at=self._now, data={"name": name})

    def post_message(self, channel: str, *, text: str, blocks: list[dict[str, Any]],
                      metadata: dict[str, Any]) -> SlackTs:
        self._require_channel(channel, require_member=True)
        self._apply_rate_limit_or_raise("post_message")
        new_ts = self._mint_ts()
        self._append_event("post", channel=channel, ts=new_ts, actor=self.bot_user_id,
                            at=self._now,
                            data={"text": text, "files": [], "blocks": blocks,
                                  "attachments": None, "metadata": metadata,
                                  "subtype": "bot_message", "bot_message": True})
        lose_fault = self._find_fault("lose_post_response", method="post_message")
        if lose_fault is not None:
            self._consume(lose_fault)
            raise SlackTransportError("lose_post_response")
        return new_ts

    def update_message(self, channel: str, ts: SlackTs, *, text: str,
                        blocks: list[dict[str, Any]], metadata: dict[str, Any]) -> SlackTs:
        self._require_channel(channel, require_member=True)
        self._apply_rate_limit_or_raise("update_message")
        rec = self._get_message(channel, ts, enforce_horizon=False)
        # Real chat.update with the bot token refuses a message the bot did
        # not post (10 section 5 Fidelity).
        if rec["user"] != self.bot_user_id:
            raise _mk_api_error("cant_update_message")
        self._append_event("edit", channel=channel, ts=ts, actor=self.bot_user_id,
                            at=self._now,
                            data={"text": text, "files": None, "blocks": blocks,
                                  "metadata": metadata})
        lose_fault = self._find_fault("lose_post_response", method="update_message")
        if lose_fault is not None:
            self._consume(lose_fault)
            raise SlackTransportError("lose_post_response")
        return ts

    def users_list(self) -> list[RawUser]:
        self._apply_rate_limit_or_raise("users_list")
        out = []
        for u in self.users.values():
            out.append({
                "id": u.id, "deleted": u.deleted, "is_bot": u.is_bot,
                "profile": {"display_name": u.display_name, "real_name": u.real_name},
            })
        return out

    def channel_info(self, channel: str) -> RawChannel:
        self._require_channel(channel, require_member=False)
        self._apply_rate_limit_or_raise("channel_info")
        meta = self.channel_meta.get(channel, {})
        return {"id": channel, "is_member": channel in self.bot_member_of,
                "is_private": bool(meta.get("is_private", False)),
                "name": str(meta.get("name", ""))}

    def conversations_members(self, channel: str) -> list[str]:
        self._require_channel(channel, require_member=False)
        self._apply_rate_limit_or_raise("conversations_members")
        if self.channel_members is not None:
            members = set(self.channel_members.get(channel, ()))
        else:
            members = set(self.users.keys())
        if channel in self.bot_member_of:
            members.add(self.bot_user_id)
        return sorted(members)

    def fetch_file_bytes(self, url: str) -> bytes:
        self._apply_rate_limit_or_raise("fetch_file_bytes")

        timeout_fault = self._find_fault("fetch_timeout")
        if timeout_fault is not None:
            self._consume(timeout_fault)
            raise SlackTransportError("fetch_timeout")

        fault_429 = self._find_fault("fetch_429")
        if fault_429 is not None:
            self._consume(fault_429)
            raise _mk_rate_limited(fault_429["retry_after_seconds"], "fetch_file_bytes")

        oversize_fault = self._find_fault("fetch_oversize")
        if oversize_fault is not None:
            self._consume(oversize_fault)
            raise _mk_file_too_large(oversize_fault["limit_bytes"])

        truncate_fault = self._find_fault("fetch_truncate")
        body = self._find_file_bytes(url)
        if body is None:
            raise _mk_http_error(404)
        if truncate_fault is not None:
            self._consume(truncate_fault)
            return body[: len(body) // 2]
        return body

    # -- authoring API (ground-truth event timeline) ---------------------------

    def post(self, *, at: SlackTs, user: str, channel: str, text: str = "",
             files: Sequence[dict] | None = None, blocks: list[dict] | None = None,
             attachments: Sequence[dict] | None = None, metadata: dict | None = None,
             subtype: str | None = None) -> SlackTs:
        self._append_event("post", channel=channel, ts=at, actor=user, at=at,
                            data={"text": text, "files": list(files) if files else [],
                                  "blocks": blocks,
                                  "attachments": list(attachments) if attachments else None,
                                  "metadata": metadata, "subtype": subtype,
                                  "bot_message": False})
        return at

    def edit(self, *, at: SlackTs, ts: SlackTs, channel: str, user: str,
              text: str | None = None, files: Sequence[dict] | None = None,
              blocks: list[dict] | None = None) -> None:
        self._append_event("edit", channel=channel, ts=ts, actor=user, at=at,
                            data={"text": text,
                                  "files": list(files) if files is not None else None,
                                  "blocks": blocks, "metadata": None})

    def delete_message(self, *, at: SlackTs, ts: SlackTs, channel: str) -> None:
        self._append_event("delete_message", channel=channel, ts=ts, actor=None, at=at, data={})

    def delete_file(self, *, at: SlackTs, ts: SlackTs, channel: str, file_index: int = 0) -> None:
        self._append_event("delete_file", channel=channel, ts=ts, actor=None, at=at,
                            data={"file_index": file_index})

    def react(self, *, at: SlackTs, ts: SlackTs, channel: str, user: str, name: str) -> None:
        self._append_event("react", channel=channel, ts=ts, actor=user, at=at,
                            data={"name": name})

    def unreact(self, *, at: SlackTs, ts: SlackTs, channel: str, user: str, name: str) -> None:
        self._append_event("unreact", channel=channel, ts=ts, actor=user, at=at,
                            data={"name": name})

    def reply(self, *, at: SlackTs, user: str, channel: str, parent_ts: SlackTs,
               text: str = "", files: Sequence[dict] | None = None,
               broadcast: bool = False) -> SlackTs:
        self._append_event("reply", channel=channel, ts=at, actor=user, at=at,
                            data={"parent_ts": parent_ts, "text": text,
                                  "files": list(files) if files else [],
                                  "broadcast": broadcast})
        return at

    def as_of(self, now: SlackTs) -> None:
        self._now = now

    # -- world (de)serialization, shared with FileBackedFakeSlack --------------

    def to_world_dict(self) -> dict[str, Any]:
        return {
            "state_version": 1,
            "now": self._now,
            "bot_user_id": self.bot_user_id,
            "horizon_days": self._horizon_days,
            "reaction_users_limit": self._reaction_users_limit,
            "users": {
                uid: {"is_bot": u.is_bot, "deleted": u.deleted,
                      "display_name": u.display_name, "real_name": u.real_name}
                for uid, u in self.users.items()
            },
            "channels": {
                c: {"is_member": c in self.bot_member_of,
                    "is_private": bool(self.channel_meta.get(c, {}).get("is_private", False)),
                    "name": str(self.channel_meta.get(c, {}).get("name", "")),
                    "members": (None if self.channel_members is None
                                else list(self.channel_members.get(c, ())))}
                for c in self.channels
            },
            "events": [_encode_event(e) for e in self._events],
            "faults": [dict(f) for f in self._faults],
        }

    @classmethod
    def from_world_dict(cls, world: dict[str, Any]) -> "FakeSlack":
        users = {
            uid: FakeUser(id=uid, is_bot=v["is_bot"], deleted=v["deleted"],
                          display_name=v["display_name"], real_name=v["real_name"])
            for uid, v in world["users"].items()
        }
        channels = list(world["channels"].keys())
        bot_member_of = [c for c, v in world["channels"].items() if v["is_member"]]
        channel_meta = {
            c: {"is_private": v.get("is_private", False), "name": v.get("name", "")}
            for c, v in world["channels"].items()
        }
        channel_members_raw = {c: v.get("members") for c, v in world["channels"].items()}
        channel_members = (
            None if all(m is None for m in channel_members_raw.values())
            else {c: list(m) for c, m in channel_members_raw.items() if m is not None}
        )
        slack = cls(
            now=world["now"], bot_user_id=world["bot_user_id"], users=users,
            channels=channels, bot_member_of=bot_member_of,
            channel_meta=channel_meta, channel_members=channel_members,
            horizon_days=world["horizon_days"],
            reaction_users_limit=world["reaction_users_limit"],
        )
        slack._events = [_decode_event(e) for e in world["events"]]
        slack._faults = [dict(f) for f in world["faults"]]
        return slack


class FileBackedFakeSlack:  # satisfies SlackIO; wraps FakeSlack
    """Every method acquires the lock, loads the world from `path`, delegates
    to a freshly-rebuilt in-memory FakeSlack, writes the world back atomically
    (so fault consumption and mutations both survive a kill), then releases."""

    def __init__(self, *, path: str) -> None:
        self._path = Path(path)

    def auth_identity(self) -> AuthIdentity:
        return self._call("auth_identity", (), {})

    def history(self, channel: str, oldest: SlackTs, latest: SlackTs | None = None) -> list[RawMessage]:
        return self._call("history", (channel, oldest), {"latest": latest})

    def reactions_get(self, channel: str, ts: SlackTs) -> RawMessage:
        return self._call("reactions_get", (channel, ts), {})

    def reactions_add(self, channel: str, ts: SlackTs, name: str) -> None:
        return self._call("reactions_add", (channel, ts, name), {})

    def reactions_remove(self, channel: str, ts: SlackTs, name: str) -> None:
        return self._call("reactions_remove", (channel, ts, name), {})

    def post_message(self, channel: str, *, text: str, blocks: list[dict[str, Any]],
                      metadata: dict[str, Any]) -> SlackTs:
        return self._call("post_message", (channel,),
                           {"text": text, "blocks": blocks, "metadata": metadata})

    def update_message(self, channel: str, ts: SlackTs, *, text: str,
                        blocks: list[dict[str, Any]], metadata: dict[str, Any]) -> SlackTs:
        return self._call("update_message", (channel, ts),
                           {"text": text, "blocks": blocks, "metadata": metadata})

    def users_list(self) -> list[RawUser]:
        return self._call("users_list", (), {})

    def channel_info(self, channel: str) -> RawChannel:
        return self._call("channel_info", (channel,), {})

    def conversations_members(self, channel: str) -> list[str]:
        return self._call("conversations_members", (channel,), {})

    def fetch_file_bytes(self, url: str) -> bytes:
        return self._call("fetch_file_bytes", (url,), {})

    def _call(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        lock_path = self._acquire_lock()
        try:
            inner = FakeSlack.from_world_dict(self._load())
            try:
                result = getattr(inner, name)(*args, **kwargs)
            except BaseException:
                self._save(inner.to_world_dict())
                raise
            self._save(inner.to_world_dict())
            return result
        finally:
            self._release_lock(lock_path)

    def _load(self) -> dict[str, Any]:
        with self._path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, world: dict[str, Any]) -> None:
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps(world, ensure_ascii=True, separators=(",", ":"))
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self._path)

    def _acquire_lock(self) -> str:
        lock_path = str(self._path) + ".lock"
        deadline = time.monotonic() + 30
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return lock_path
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"timed out acquiring fake-slack world lock: {lock_path}")
                time.sleep(0.02)

    def _release_lock(self, lock_path: str) -> None:
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
