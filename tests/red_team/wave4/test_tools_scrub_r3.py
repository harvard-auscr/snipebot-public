"""Breaker round 3, surface tools-scrub: tools/scrub.py and tools/capture_fixtures.py.

Every payload here is hand-built and offline. Raw (pre-scrub) ids are
synthetic ids shaped like the real workspace's (11 characters, `X0...`);
raw names and URLs are synthetic strings (`Rawfirst`, `Rawlast`,
`raw-space.example.com`). Nothing touches the network; the capture-tool test
drives a fake client and writes only under tmp_path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snipebot.config import Section
from snipebot.report import _row_line
from tools import capture_fixtures as cf
from tools import scrub as scrub_mod
from tools.scrub import FIXTURES_DIR, SCRUB_MAP_PATH, load_scrub_map, no_real_identifiers, scrub

RAW_SNIPER = "U0RAWSNIP01"
RAW_TARGET = "U0RAWTARG02"
RAW_NEWCOMER = "U0RAWNEWU05"
RAW_BOT_USER = "U0RAWBOT001"
RAW_BOT_ID = "B0RAWBOT001"
RAW_TEAM = "T0RAWTEAM01"
RAW_MAIN = "C0RAWMAIN01"
RAW_OLD_MAIN = "C0RAWOLDM01"
RAW_OFFICERS = "C0RAWOFFC01"

_PLACEHOLDER_RE = re.compile(r"\b(U0AAA\d{3}|F0FILE\d{3})\b")


def _placeholders_used_by_committed_fixtures() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for sub in ("history", "refetch", "reactions", "controls", "users", "channels"):
        d = FIXTURES_DIR / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.json")):
            for m in _PLACEHOLDER_RE.finditer(p.read_text(encoding="utf-8")):
                used.setdefault(m.group(1), []).append(str(p.relative_to(FIXTURES_DIR)))
    return used


# --- 1. _seed_roles wipes the fixture-placeholder reservations ------------------

def test_seed_roles_on_a_fresh_clone_forgets_placeholders_committed_fixtures_use():
    """Claim: on a machine without the gitignored scrub_map.local.json (a fresh
    clone, spec 10 section 8 / E-G2-2: the local half is never committed),
    load_scrub_map returns only identity entries (the committed vocabulary plus
    every placeholder the fixtures already use). _seed_roles then sees an
    all-identity map and clear()s it, throwing away exactly the reservations
    the round-2 repair added. The next real upload in the run scrubs to
    F0FILE001, then F0FILE002 (already used by history/ and refetch/
    fixtures), and a new real member scrubs to U0AAA003 (the second-target
    role committed fixtures use). One placeholder then names two different
    real files / people across fixtures. Violates spec 10 section 8 ("stable
    per real id"; "one deterministic map ... so cross-references stay
    consistent within and across files")."""
    used = _placeholders_used_by_committed_fixtures()
    assert "F0FILE002" in used and "U0AAA003" in used, "fixture vocabulary changed"

    working = load_scrub_map(SCRUB_MAP_PATH, local_path=None)
    # run_capture today: one admin, so second/third targets default to the first.
    cf._seed_roles(
        working, sniper=RAW_SNIPER, targets=(RAW_TARGET, RAW_TARGET, RAW_TARGET),
        bot_user=RAW_BOT_USER, bot_id=RAW_BOT_ID, team=RAW_TEAM,
        main_channel=RAW_MAIN, officers_channel=RAW_OFFICERS,
    )
    out = scrub(
        {"type": "message", "user": RAW_SNIPER, "ts": "1790210002.000100",
         "text": f"got <@{RAW_TARGET}> <@{RAW_NEWCOMER}>",
         "files": [
             {"id": "F0RAWNEW001", "mimetype": "image/jpeg", "filetype": "jpg", "name": "IMG_1.jpg"},
             {"id": "F0RAWNEW002", "mimetype": "image/jpeg", "filetype": "jpg", "name": "IMG_2.jpg"},
         ]},
        working,
    )
    new_ids = [f["id"] for f in out["files"]] + [working[RAW_NEWCOMER]]
    clashes = {i: sorted(set(used[i])) for i in new_ids if i in used}
    assert not clashes, f"new real ids scrubbed onto placeholders committed fixtures use: {clashes}"


# --- 2. a display name holding " — " leaks through the digest scrub -------------

def test_capture_digest_leaks_the_tail_of_a_display_name_containing_an_em_dash():
    """Claim: _scrub_digest_names finds a standings name with the non-greedy
    `^(\\d+\\. )(.+?)( — )`, so it stops at the FIRST " — ". report._row_line
    prints '1. <name> — 3 pts (3 snipes)', and a display name that itself
    holds " — " (e.g. 'Rawfirst — Rawlast') keeps everything after its own
    dash: the committed history/digest.json would read '1. user-1 — Rawlast —
    3 pts (3 snipes)'. no_real_identifiers cannot see names in digest text,
    so the capture writes it. Violates spec 10 section 8 ("display / real
    names ... -> empty string or user-<n> (never a real name)")."""
    row = SimpleNamespace(person=RAW_SNIPER, points=3, snipes_made=3)
    line = _row_line(Section.TOP_SNIPERS, row, 1, {RAW_SNIPER: "Rawfirst — Rawlast"})
    msg = {
        "type": "message", "subtype": "bot_message", "text": "Snipes: Thu Sep 18",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Top snipers*\n{line}"}}],
        "metadata": {"event_type": "snipe_digest", "event_payload": {"report": "daily"}},
    }
    scrubbed = cf._scrub_digest_names(msg)
    text = json.dumps(scrubbed, ensure_ascii=False)
    assert "Rawlast" not in text and "Rawfirst" not in text, text


# --- 3. the verifier never looks at dict keys -----------------------------------

def test_no_real_identifiers_misses_a_real_channel_id_used_as_a_dict_key():
    """Claim: no_real_identifiers walks dict VALUES only; keys are never
    checked. Slack keys some objects by channel id: a pinned message's
    `pinned_info` is {"<channel id>": {"pinned_by": ..., "pinned_ts": ...}}.
    scrub() does not rewrite keys either, so a real channel id there is
    written into a committed fixture while the verifier reports the payload
    clean. The verifier's contract (tools/scrub.py docstring; spec 10 section
    8 "channel ids (C.../G...) -> C0MAIN01, C0OFF001, ... stable per real id")
    is that a real-looking id anywhere in the payload is a problem."""
    payload = {
        "type": "message", "user": "U0AAA001", "ts": "1790210003.000100",
        "pinned_to": ["C0MAIN01"],
        "pinned_info": {RAW_MAIN: {"pinned_by": "U0AAA001", "pinned_ts": 1790210004}},
    }
    problems = no_real_identifiers(payload, vocabulary=["U0AAA001", "C0MAIN01"])
    assert any(RAW_MAIN in p for p in problems), problems


# --- 6. capture_channels bypasses the role guard -------------------------------

class _ChannelsBotClient:
    def conversations_info(self, channel: str, **_: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "channel": {
                "id": channel, "created": 1790197593, "creator": RAW_SNIPER,
                "is_channel": True, "is_private": False, "is_member": True,
                "name": "snipes", "context_team_id": RAW_TEAM,
                "topic": {"value": "", "creator": "", "last_set": 0},
                "purpose": {"value": "", "creator": "", "last_set": 0},
                "shared_team_ids": [RAW_TEAM], "previous_names": [],
            },
        }


def test_capture_channels_gives_a_second_real_channel_the_taken_main_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Claim: capture_channels pins the roles with
    `scrub_map.setdefault(main_channel, "C0MAIN01")` (and the same for
    C0OFF001), bypassing the `placeholder not in taken` guard _seed_roles
    uses. When the local map already gives C0MAIN01 to an earlier real
    channel (the watched channel was re-created or `channel:` changed between
    capture runs), _seed_roles correctly refuses to reuse it, but
    capture_channels then maps the new real channel to C0MAIN01 as well:
    two real ids share one placeholder, and channels/main.json's C0MAIN01 is
    not the channel the older fixtures' C0MAIN01 names. Violates spec 10
    section 8 (one deterministic map, "stable per real id", cross-references
    consistent across files)."""
    committed = tmp_path / "scrub_map.json"
    local = tmp_path / "scrub_map.local.json"
    seed = {"C0MAIN01": "C0MAIN01", "C0OFF001": "C0OFF001", "U0AAA001": "U0AAA001",
            "T0TEAM": "T0TEAM", RAW_OLD_MAIN: "C0MAIN01", RAW_SNIPER: "U0AAA001",
            RAW_TEAM: "T0TEAM"}
    scrub_mod.save_scrub_map(seed, committed, note="n", local_path=local)

    def _load() -> dict[str, str]:
        return scrub_mod.load_scrub_map(committed, local_path=local)

    def _save(scrub_map: dict[str, str], path: Path | None = None, *, note: str | None = None,
              local_path: Path | None = None) -> None:
        scrub_mod.save_scrub_map(scrub_map, committed, note=note, local_path=local)

    monkeypatch.setattr(cf, "FIXTURES_DIR", tmp_path)
    monkeypatch.setattr(cf, "load_scrub_map", _load)
    monkeypatch.setattr(cf, "save_scrub_map", _save)

    cf.capture_channels(_ChannelsBotClient(), RAW_MAIN, None)

    real = json.loads(local.read_text(encoding="utf-8"))
    holders = sorted(k for k, v in real.items() if v == "C0MAIN01" and k != "_note")
    assert len(holders) == 1, f"C0MAIN01 now names {len(holders)} real channels"
