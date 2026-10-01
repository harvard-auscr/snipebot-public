"""Parsed Slack facts: the shared data types and the parse contract.

Module home of the parsed types shared across the package (00-data §2, §9; 10
§4): `VetoSource`, `Veto`, `TargetEdit`, `SelfieOverride`, `Candidate`,
`file_sig`, `Digest`, `ParseAnomaly`, `DigestMetadata`, and the `parse` /
`rendition_url` selectors. Other modules import these by reference and never
redefine them.

`parse`, `rendition_url` and `file_sig` are declared here but filled in by the
parser workstream; their bodies raise until then.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # raw Slack message dict; homed in snipebot/slack_io.py (10 §1)
    from snipebot.slack_io import RawMessage


class VetoSource(str, Enum):
    REACTION = "reaction"   # veto emoji observed on the message this run
    CLI = "cli"             # `snipebot veto` / manual workflow; durable, no deadline


@dataclass(frozen=True)
class Veto:
    by: str                 # user ID of the actor who cast the veto
    source: VetoSource


@dataclass(frozen=True)
class SelfieOverride:
    """A durable admin ruling on a sibfam selfie that beats the detector either
    way (drives §4 classification). `VetoSource` is reused as the "how the fact
    entered" enum.

    - `source == VetoSource.REACTION` (an admin's selfie emoji seen in the scan
      window): writes `value=True` only if no override exists yet (first-seen,
      like an opt-out); removing the reaction later changes nothing. A reaction
      never writes `False` and never replaces a `CLI` override.
    - `source == VetoSource.CLI` (`snipebot selfie --ts [--no]`): writes
      `True`/`False` and replaces any existing override, reaction-sourced or not.
    """
    value: bool             # True = selfie (award participation points); False = plain snipe
    by: str                 # user ID of the admin who set it
    source: VetoSource


@dataclass(frozen=True)
class TargetEdit:
    """First-seen record for a target that was NOT present when the row was created."""
    user: str
    edit_ts: str | None     # the message's last_edit_ts at the sync that first saw
                            # this target; None if unedited at that sync.


@dataclass(frozen=True)
class Candidate:
    # identity / structure
    ts: str                             # Slack ts of the message; primary key
    sender: str                         # user ID
    subtype: str | None                 # e.g. "file_share", "thread_broadcast"; None if plain
    thread_ts: str | None               # Slack ts; None when absent

    # wholesale-replaced facts (latest observation)
    targets: tuple[str, ...]            # current real-user @-mentions, first-appearance
                                        # order, deduped; excludes @channel/@here/usergroups
    live_images: int                    # live uploaded images
    live_image_ids: tuple[str, ...]     # file ids of the CURRENT live images, sorted asc;
                                        # face_counts / rendition_hash are keyed by these
    live_videos: int                    # live uploaded videos
    linked_images: int                  # image URLs pasted as links (not uploads)
    last_edit_ts: str | None            # Slack ts of the last edit; None if never edited
    file_sigs: tuple[str, ...]          # repost signatures, sorted ascending
    vetoes: tuple[Veto, ...]
    missing_runs: int                   # consecutive complete fetches that did not return it

    # first-seen facts (accumulate, never overwritten)
    first_seen_targets: frozenset[str]  # real-user targets present when the row was created
    first_sight_edited: bool            # was the message already edited when first recorded
    target_edited_in: tuple[TargetEdit, ...]   # one per target NOT in first_seen_targets that
                                        # later appeared; sorted by user

    # faces / selfie facts (filled by `sync`, never `parse`; §3 keys 17-20)
    face_counts: Mapping[str, int] = field(default_factory=dict)      # file id -> face count
    rendition_hash: Mapping[str, str] = field(default_factory=dict)   # file id -> hex SHA-256
    detect_attempts: int = 0            # runs that tried detection and left >= 1 live image
                                        # uncounted; only ever increments
    selfie_override: "SelfieOverride | None" = None   # durable admin ruling (§4); None until set

    # transient: whether the message carried ANY file object at first sight, tombstoned or
    # not. Distinguishes a text-only post (drop) from an image post whose file was already
    # tombstoned when a schedule first saw it (keep, with live_images==0). live_images /
    # live_image_ids / file_sigs all exclude tombstoned files, so none can carry this.
    # Not serialized to the ledger and excluded from equality (the keep gate at merge only
    # reads it for freshly parsed rows; stored rows never revisit it).
    has_file_object: bool = field(default=False, compare=False, repr=False)

    @property
    def deleted(self) -> bool:
        """Two consecutive complete fetches missing the row."""
        return self.missing_runs >= 2

    @property
    def is_top_level(self) -> bool:
        """A parent with replies carries thread_ts == ts and is still top-level."""
        return self.thread_ts is None or self.thread_ts == self.ts


def file_sig(name: str, size: int, width: int | None, height: int | None) -> str:
    """Repost signature for one uploaded file: lowercase-hex SHA-256 of name,
    size and pixel dimensions. The name itself is never stored, only the hash."""
    w = "" if width is None else str(width)
    h = "" if height is None else str(height)
    payload = f"{name}\x1f{size}\x1f{w}\x1f{h}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ParseAnomaly(Warning):
    """Text/blocks mention disagreement, or a digest whose payload.channel != the
    channel it was found in. Logged with IDs only; parse still returns a value."""


@dataclass(frozen=True)
class Digest:
    ts: str                 # Slack ts of the digest MESSAGE
    channel: str            # the channel arg parse() was called with (where it was found)
    report: str             # event_payload.report
    period_key: str         # event_payload.period_key   (00-data §8)
    semester: str           # event_payload.semester
    numbers_hash: str       # event_payload.numbers_hash  (00-data §9)
    revision: int           # event_payload.revision


@dataclass(frozen=True)
class DigestMetadata:
    event_type: str          # constant "snipe_digest"
    # event_payload:
    report: str              # report name, e.g. "daily"
    period_key: str          # §8, e.g. "daily:2026-09-18"
    channel: str             # channel ID the digest was posted to
    semester: str            # semester name the digest covers
    numbers_hash: str        # sha256 hex
    revision: int            # 0 on first post; +1 on each numbers-changing chat.update

    def to_wire(self, channel: str) -> dict:
        """DigestMetadata -> the Slack wire dict. `channel` is where the digest is
        being posted; it overrides `self.channel`."""
        return {
            "event_type": "snipe_digest",
            "event_payload": {
                "report": self.report,
                "period_key": self.period_key,
                "channel": channel,
                "semester": self.semester,
                "numbers_hash": self.numbers_hash,
                "revision": self.revision,
            },
        }

    @staticmethod
    def from_wire(channel: str, wire: dict) -> "DigestMetadata":
        """The inverse, used by `parse` on a fetched digest. `channel` is the
        channel the message was found in and wins over `event_payload.channel`."""
        payload = wire["event_payload"]
        return DigestMetadata(
            event_type=wire["event_type"],
            report=payload["report"],
            period_key=payload["period_key"],
            channel=channel,
            semester=payload["semester"],
            numbers_hash=payload["numbers_hash"],
            revision=payload["revision"],
        )


# A real-user mention: `<@U…>` / `<@W…>`, with the deprecated `<@U…|label>` label
# discarded. `<!channel>`, `<!here>`, `<!subteam^S…>` and `<#C…>` never match.
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
# A fence runs to its closing ``` or, when unclosed, to the end of the message (Slack
# renders it as a code block either way); an inline span never crosses a line break.
_FENCED_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_RE = re.compile(r"`[^`\n]*`")

_RENDITION_KEYS = ("thumb_1024", "thumb_960", "thumb_720", "thumb_480", "url_private_download")
_POST_SHAPE_SUBTYPES = frozenset({None, "file_share", "thread_broadcast"})


def _is_present(f: dict) -> bool:
    """An uploaded file Slack still serves: not tombstoned, not hidden by the free-plan
    cap, not a Slack Connect stub (10 §4)."""
    return (
        f.get("is_tombstoned") is not True
        and f.get("mode") != "hidden_by_limit"
        and f.get("file_access") != "check_file_info"
    )


def _is_live(f: dict) -> bool:
    """A live image: an `image/*` upload that is still present (10 §4)."""
    return str(f.get("mimetype", "")).startswith("image/") and _is_present(f)


def _is_live_video(f: dict) -> bool:
    """A live video: a `video/*` upload that is still present (10 §4)."""
    return str(f.get("mimetype", "")).startswith("video/") and _is_present(f)


def _strip_code(text: str) -> str:
    """Drop fenced code blocks first, then inline code spans (10 §4). Slack never
    pings a mention inside code, so these are removed before mention scanning.
    Blockquote lines are left intact — Slack does ping a quoted mention."""
    text = _FENCED_RE.sub("", text)
    text = _INLINE_RE.sub("", text)
    return text


def _text_mentions(text: str) -> list[str]:
    """Real-user ids from the message text, first-appearance order, deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _MENTION_RE.finditer(_strip_code(text)):
        uid = match.group(1)
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def _block_mentions(blocks: object) -> list[str]:
    """Every `{"type": "user", "user_id": …}` id in the rich_text blocks, document
    order. A usergroup/broadcast/link element is not a `user` element, so it is
    never collected."""
    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "user" and "user_id" in node:
                out.append(node["user_id"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return out


def rendition_url(file: dict) -> str | None:
    """Pick one URL from a raw Slack file object by the rendition preference
    order, or None when the file carries none of them. Pure, no I/O."""
    for key in _RENDITION_KEYS:
        value = file.get(key)
        if isinstance(value, str) and value.strip():
            return value   # an empty or blank value is not a URL the file carries
    return None


def parse(message: "RawMessage", channel: str, bot_user_id: str) -> "Candidate | Digest | None":
    """One observation of one message -> a Candidate, a Digest, or None. Pure:
    raw dict in, one dataclass or None out, no I/O, no clock."""
    # 1. Digest first: a `snipe_digest` metadata message, regardless of sender.
    metadata = message.get("metadata") or {}
    if metadata.get("event_type") == "snipe_digest":
        if "bot_id" not in message and message.get("user") != bot_user_id:
            warnings.warn(
                ParseAnomaly(f"digest not from bot ts={message.get('ts')} user={message.get('user')}")
            )
        payload = metadata.get("event_payload")
        # A malformed payload (another integration reusing the event type, or an
        # older shape) is an anomaly, never a failed run (20 §2 step 3).
        if not (
            isinstance(payload, dict)
            and all(isinstance(payload.get(k), str)
                    for k in ("report", "period_key", "semester", "numbers_hash"))
            and isinstance(payload.get("revision"), int)
            and not isinstance(payload.get("revision"), bool)
        ):
            warnings.warn(
                ParseAnomaly(f"digest malformed ts={message.get('ts')} user={message.get('user')}")
            )
            return None
        if payload.get("channel") != channel:
            warnings.warn(
                ParseAnomaly(
                    f"digest channel mismatch ts={message.get('ts')} "
                    f"found={channel} payload={payload.get('channel')}"
                )
            )
        meta = DigestMetadata.from_wire(channel, metadata)
        return Digest(
            ts=message["ts"],
            channel=meta.channel,
            report=meta.report,
            period_key=meta.period_key,
            semester=meta.semester,
            numbers_hash=meta.numbers_hash,
            revision=meta.revision,
        )

    # 2. Human test: a human `user`, not a bot_message subtype, not the bot's
    # own id. A `bot_id` alone does not disqualify: a message a person posts
    # through an app's user token carries `bot_id` + `bot_profile` next to
    # their real `user` (10 §4 E-G2-1), and that person is the sender.
    user = message.get("user")
    if (
        not isinstance(user, str)
        or not user
        or message.get("subtype") == "bot_message"
        or user == bot_user_id
    ):
        return None

    # 3. Post-shape test.
    subtype = message.get("subtype")
    if subtype not in _POST_SHAPE_SUBTYPES:
        return None

    # 4. Build the single-observation Candidate.
    files = message.get("files", [])

    live_image_ids = tuple(sorted(f["id"] for f in files if _is_live(f)))
    live_images = len(live_image_ids)
    live_videos = sum(1 for f in files if _is_live_video(f))

    attachments = message.get("attachments", [])
    linked_images = sum(1 for a in attachments if "image_url" in a)

    file_sigs = tuple(sorted(
        file_sig(f["name"], f["size"], f.get("original_w"), f.get("original_h"))
        for f in files
        if str(f.get("mimetype", "")).startswith("image/") and f.get("is_tombstoned") is not True
    ))

    text_mentions = _text_mentions(message.get("text", ""))
    blocks = message.get("blocks")
    if blocks is not None:
        block_mentions = _block_mentions(blocks)
        if set(text_mentions) != set(block_mentions):
            warnings.warn(
                ParseAnomaly(
                    f"mention text/blocks disagree ts={message.get('ts')} "
                    f"text={sorted(set(text_mentions))} blocks={sorted(set(block_mentions))}"
                )
            )
    targets = tuple(text_mentions)

    return Candidate(
        ts=message["ts"],
        sender=message["user"],
        subtype=subtype,
        thread_ts=message.get("thread_ts"),
        targets=targets,
        live_images=live_images,
        live_image_ids=live_image_ids,
        live_videos=live_videos,
        linked_images=linked_images,
        last_edit_ts=message.get("edited", {}).get("ts"),
        file_sigs=file_sigs,
        vetoes=(),
        missing_runs=0,
        first_seen_targets=frozenset(targets),
        first_sight_edited="edited" in message,
        target_edited_in=(),
        has_file_object=bool(files),
    )
