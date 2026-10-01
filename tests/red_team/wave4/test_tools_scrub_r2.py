"""Breaker round 2, surface tools-scrub: tools/scrub.py and tools/capture_fixtures.py.

Every payload here is hand-built and offline. Raw (pre-scrub) ids are
synthetic ids shaped like the real workspace's (11 characters, `X0...`);
raw names are synthetic strings (`Rawfirst`, `Rawlast`, `rawdisplay`).
Nothing touches the network; the capture-tool tests drive fake clients and
write only under tmp_path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools import scrub as scrub_mod
from tools.scrub import FIXTURES_DIR, SCRUB_MAP_PATH, load_scrub_map, scrub

RAW_SNIPER = "U0RAWSNIP01"
RAW_TARGET_A = "U0RAWTARG02"
RAW_TARGET_B = "U0RAWTARG03"
RAW_TARGET_NEW = "U0RAWTARG04"
RAW_BOT_USER = "U0RAWBOT001"
RAW_BOT_ID = "B0RAWBOT001"
RAW_TEAM = "T0RAWTEAM01"
RAW_APP = "A0RAWAPP001"
RAW_MAIN = "C0RAWMAIN01"
RAW_OFFICERS = "C0RAWOFFC01"

# The committed placeholder vocabulary (tests/fixtures/scrub_map.json): each
# placeholder mapped to itself.
_VOCABULARY = (
    "A0APP", "B0BOT", "B0BOT2", "C0MAIN01", "C0OFF001",
    "F0FILE001", "F0FILE002", "F0FILE003", "F0FILE004", "S0TEAM01", "T0TEAM",
    "U0AAA001", "U0AAA002", "U0AAA003", "U0AAA004", "U0AAA009", "U0AAA010",
    "U0AAA011", "U0AAA099", "U0BOT01",
)


def _redirect_capture_io(cf: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                         seed: dict[str, str]) -> None:
    committed = tmp_path / "scrub_map.json"
    local = tmp_path / "scrub_map.local.json"
    scrub_mod.save_scrub_map(seed, committed, note="n", local_path=local)

    def _load() -> dict[str, str]:
        return scrub_mod.load_scrub_map(committed, local_path=local)

    def _save(scrub_map: dict[str, str], path: Path | None = None, *, note: str | None = None,
              local_path: Path | None = None) -> None:
        scrub_mod.save_scrub_map(scrub_map, committed, note=note, local_path=local)

    monkeypatch.setattr(cf, "FIXTURES_DIR", tmp_path)
    monkeypatch.setattr(cf, "PROVISIONAL_MD", tmp_path / "PROVISIONAL.md")
    monkeypatch.setattr(cf, "CAPTURED_MD", tmp_path / "CAPTURED.md")
    monkeypatch.setattr(cf, "load_scrub_map", _load)
    monkeypatch.setattr(cf, "save_scrub_map", _save)
    monkeypatch.setattr(cf.time, "sleep", lambda s: None)


# --- 1. a captured digest commits the players' display names -------------------

class _DigestBotClient:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message

    def conversations_history(self, channel: str, limit: int = 50, **_: Any) -> dict[str, Any]:
        return {"ok": True, "messages": [self._message]}


def test_capture_digest_commits_real_display_names_from_the_standings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Claim: capture_digest() writes a real posted digest to history/digest.json
    through scrub() + no_real_identifiers(), but a digest's standings print
    plain display names (report.py _row_line: "1. <name> - 3 pts ...", never a
    `<@...>` mention). scrub() rewrites only ids, mentions and URLs inside
    `text`, and the verifier checks names only in users.list entries,
    user_profile, author_* and username, so every player's real display name
    (and a sibling group's name) lands verbatim in the committed fixture.
    Violates spec 10 section 8 ID-scrub rules ("display / real names ... ->
    empty string or `user-<n>` (never a real name)") and feed item 15 (the G2
    name leak class: names must never reach a committed fixture)."""
    from tools import capture_fixtures as cf

    seed = {RAW_BOT_USER: "U0BOT01", RAW_BOT_ID: "B0BOT", RAW_TEAM: "T0TEAM",
            RAW_MAIN: "C0MAIN01", RAW_APP: "A0APP"}
    _redirect_capture_io(cf, tmp_path, monkeypatch, seed)

    digest_msg = {
        "type": "message",
        "subtype": "bot_message",
        "bot_id": RAW_BOT_ID,
        "app_id": RAW_APP,
        "user": RAW_BOT_USER,
        "team": RAW_TEAM,
        "text": "Daily snipe standings",
        "ts": "1790210000.001700",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Daily snipe standings"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                "*Top snipers*\n"
                "1. Rawfirst Rawlast — 3 pts (3 snipes)\n"
                "2. rawdisplay — 1 pts (1 snipes)"
            )}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                "*Top pairs*\n1. Rawfirst Rawlast → rawdisplay — 2"
            )}},
        ],
        "metadata": {
            "event_type": "snipe_digest",
            "event_payload": {
                "report": "daily", "period_key": "daily:2026-09-18", "channel": RAW_MAIN,
                "semester": "fall-2026", "numbers_hash": "0" * 64, "revision": 0,
            },
        },
    }

    captured, _note = cf.capture_digest(_DigestBotClient(digest_msg), RAW_MAIN)
    assert captured

    written = (tmp_path / "history" / "digest.json").read_text(encoding="utf-8")
    leaked = [part for part in ("Rawfirst", "Rawlast", "rawdisplay") if part in written]
    assert leaked == [], f"real display names committed in history/digest.json: {leaked}"


# --- 2. _seed_roles drops a new role onto a generic placeholder -----------------

def test_seed_roles_skips_the_pending_third_target_because_of_vocabulary_identities():
    """Claim: since E-G2-2 the working map is the committed vocabulary (each
    placeholder -> itself) merged with scrub_map.local.json, so it is never
    all-identity and _seed_roles never clears it. Its guard `placeholder not in
    scrub_map.values()` then sees every role placeholder already present (as the
    vocabulary's identity entry) and skips any role whose real id is new. The
    pending owner step (feed item 9: add the third target alias, then redo
    `--only multi-tag` / `--only mention-in-quote-or-code`) therefore maps the
    third target to U0AAA100, not U0AAA004, and the recaptured fixtures break
    the PROVISIONAL.md roles. Today's working map has no real id on U0AAA004.
    Violates tests/fixtures/PROVISIONAL.md "Roles are stable across every file"
    (targets U0AAA002-004) and spec 10 section 8 (one deterministic map)."""
    from tools import capture_fixtures as cf

    working: dict[str, str] = {p: p for p in _VOCABULARY}
    # the local half from an earlier real run: every role but the third target
    working.update({
        RAW_SNIPER: "U0AAA001", RAW_TARGET_A: "U0AAA002", RAW_TARGET_B: "U0AAA003",
        RAW_BOT_USER: "U0BOT01", RAW_BOT_ID: "B0BOT", RAW_TEAM: "T0TEAM",
        RAW_MAIN: "C0MAIN01", RAW_OFFICERS: "C0OFF001",
    })

    cf._seed_roles(
        working, sniper=RAW_SNIPER, targets=(RAW_TARGET_A, RAW_TARGET_B, RAW_TARGET_NEW),
        bot_user=RAW_BOT_USER, bot_id=RAW_BOT_ID, team=RAW_TEAM,
        main_channel=RAW_MAIN, officers_channel=RAW_OFFICERS,
    )
    out = scrub(
        {"type": "message", "user": RAW_SNIPER, "ts": "1790210001.000100",
         "text": f"got <@{RAW_TARGET_A}> <@{RAW_TARGET_B}> <@{RAW_TARGET_NEW}>"},
        working,
    )

    assert working.get(RAW_TARGET_NEW) == "U0AAA004", (
        f"third target not pinned to U0AAA004; multi-tag scrubs to {out['text']!r}"
    )


# --- 3. a new real upload reuses a placeholder a committed fixture already uses -

_FILE_ID_RE = re.compile(r'"id":\s*"(F0FILE\d{3})"')


def _file_placeholders_in_committed_fixtures() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for sub in ("history", "refetch", "reactions", "controls"):
        d = FIXTURES_DIR / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.json")):
            for m in _FILE_ID_RE.finditer(p.read_text(encoding="utf-8")):
                used.setdefault(m.group(1), []).append(str(p.relative_to(FIXTURES_DIR)))
    return used


def test_next_real_upload_gets_a_file_placeholder_a_committed_fixture_already_uses():
    """Claim: the committed vocabulary says it lists "every placeholder the
    fixtures use", but it stops at F0FILE004 while committed fixtures use
    F0FILE005..F0FILE041 (the captured history/at-channel.json among them,
    written with a throwaway map before the round-1 repair). _assign numbers a
    new file from the map's values alone, so the next real upload (the pending
    multi-tag / mention-in-quote-or-code recapture) scrubs to F0FILE005: one
    placeholder then names two different real files across fixtures, and their
    rendition URLs (https://fixture.invalid/files/F0FILE005/...) coincide.
    Violates spec 10 section 8 ("stable per real id"; "one deterministic map
    ... so cross-references stay consistent within and across files")."""
    vocabulary = load_scrub_map(SCRUB_MAP_PATH, local_path=None)
    used = _file_placeholders_in_committed_fixtures()
    assert used, "no committed fixture file ids found"

    out = scrub(
        {"type": "message", "user": "U0AAA001", "ts": "1790210002.000100", "text": "got <@U0AAA002>",
         "files": [{"id": "F0RAWNEW001", "mimetype": "image/jpeg", "filetype": "jpg",
                    "name": "IMG_0002.jpg"}]},
        dict(vocabulary),
    )
    new_id = out["files"][0]["id"]

    assert new_id not in used, (
        f"new real upload scrubbed to {new_id}, already used by {used[new_id]}"
    )
