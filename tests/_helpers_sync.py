"""Shared builders for the sync tests: a full resolved `Config`, a `FakeSlack`-authored
world, and small file/candidate helpers. Kept out of conftest so no other workstream
depends on it (only the two sync test files import this).
"""

from __future__ import annotations

import calendar
import hashlib
from pathlib import Path

from zoneinfo import ZoneInfo

from snipebot.config import (
    Cadence,
    Config,
    ConsentConfig,
    CooldownRule,
    DatedRules,
    FacesConfig,
    FeedbackReactions,
    MultiTag,
    Persistence,
    ReportSpec,
    ResolvedRule,
    ReviewFlag,
    Roster,
    RosterEntry,
    Scope,
    Section,
    Semester,
    SyncSettings,
    VetoActor,
)
from snipebot.ts import US_PER_MINUTE, US_PER_SECOND

TZ = ZoneInfo("UTC")
CHANNEL = "C0MAIN01"
BOT = "U0BOT"

SEMESTER = Semester(
    name="fall-2026",
    start_us=calendar.timegm((2026, 9, 1, 0, 0, 0, 0, 0, 0)) * US_PER_SECOND,
    end_us=calendar.timegm((2026, 12, 18, 23, 59, 59, 0, 0, 0)) * US_PER_SECOND + 999_999,
)


def secs(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def us(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return secs(y, mo, d, h, mi, s) * US_PER_SECOND


def mkts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, micro: int = 1) -> str:
    return f"{secs(y, mo, d, h, mi, s)}.{micro:06d}"


def rule(*, selfie_bonus: bool = False, cooldown_min: int = 15,
         count_image_links: bool = False, allow_video: bool = False,
         max_targets: int | None = None) -> ResolvedRule:
    return ResolvedRule(
        effective_from_us=0,
        cooldown=CooldownRule(
            microseconds=cooldown_min * US_PER_MINUTE,
            scope=Scope.PAIR,
            rejected_attempts_reset=False,
        ),
        multi_tag=MultiTag.PER_TARGET,
        max_targets_per_message=max_targets,
        edit_grace_us=10 * US_PER_MINUTE,
        max_snipes_per_target_per_day=None,
        allow_self=False,
        allow_bots=False,
        count_thread_replies=False,
        count_image_links=count_image_links,
        allow_video=allow_video,
        selfie_bonus=selfie_bonus,
    )


def roster_of(groups: dict[str, str | None], *, bots: set[str] | None = None,
              count_intra_group: bool = True) -> Roster:
    bots = bots or set()
    entries = {
        u: RosterEntry(user=u, join_us=0, group=g, is_bot=(u in bots))
        for u, g in groups.items()
    }
    return Roster(entries=entries, count_intra_group=count_intra_group)


def make_config(
    *,
    roster: Roster,
    persistence: Persistence = Persistence.FILES,
    selfie_bonus: bool = False,
    selfie_emoji: str | None = None,
    veto_by: tuple[VetoActor, ...] = (VetoActor.ADMINS,),
    admins: tuple[str, ...] = ("U0ADMIN",),
    optout_message_ts: tuple[str, ...] = (),
    seed_opted_out: tuple[str, ...] = (),
    scan_days: int = 14,
    history_horizon_days: int | None = 90,
    max_deletes_per_run: int = 5,
    large_movement_rows: int = 25,
    max_attempts: int = 3,
    review_min_targets: int | None = 5,
    review_emoji: str | None = "question",
    reports: tuple[ReportSpec, ...] | None = None,
    enabled: bool = True,
    rules: DatedRules | None = None,
) -> Config:
    if rules is None:
        rules = DatedRules(entries=(rule(selfie_bonus=selfie_bonus),))
    if reports is None:
        reports = (ReportSpec(
            name="daily", cadence=Cadence.DAILY, at_hour=21, at_minute=0,
            weekday=None, post_to=None, sections=(Section.DAY,), top_n=5,
        ),)
    return Config(
        enabled=enabled,
        persistence=persistence,
        channel=CHANNEL,
        tz=TZ,
        sync=SyncSettings(
            interval_minutes=10,
            scan_days=scan_days,
            history_horizon_days=history_horizon_days,
            max_deletes_per_run=max_deletes_per_run,
            large_movement_rows=large_movement_rows,
        ),
        semesters=(SEMESTER,),
        rules=rules,
        roster=roster,
        consent=ConsentConfig(
            veto_emoji="no_entry_sign",
            veto_by=veto_by,
            optout_message_ts=optout_message_ts,
            seed_opted_out=seed_opted_out,
        ),
        admins=admins,
        feedback=FeedbackReactions(
            counted="white_check_mark",
            cooldown="hourglass_flowing_sand",
            untagged=None,
            not_counted="x",
            selfie=selfie_emoji,
        ),
        review=ReviewFlag(min_targets=review_min_targets, emoji=review_emoji),
        reports=reports,
        faces=FacesConfig(
            model_path="models/face.onnx",
            fetch_timeout_seconds=5,
            max_image_bytes=1_000_000,
            max_attempts=max_attempts,
            score_threshold="0.9",
        ),
    )


def image_file(file_id: str, data: bytes, *, mimetype: str = "image/png",
               name: str = "snap.png") -> dict:
    """A raw Slack file dict FakeSlack will serve `data` for under `thumb_1024`."""
    return {
        "id": file_id,
        "mimetype": mimetype,
        "name": name,
        "size": len(data),
        "original_w": 100,
        "original_h": 100,
        "thumb_1024": f"https://files.example/{file_id}",
        "url_private_download": f"https://files.example/{file_id}/dl",
        "_bytes": data,
    }


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def data_paths(tmp_path: Path) -> tuple[Path, Path]:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"
