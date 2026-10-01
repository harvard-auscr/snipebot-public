"""Wave 4, round 1 probes for the report/export surface (``snipebot.report``).

Each test builds an ``Eligibility`` by hand and renders a digest with
``render_digest``. A test here is expected to FAIL against the current code: its
docstring states the claim and the spec sentence it rests on.
"""

from __future__ import annotations

import re

from snipebot.aggregate import Eligibility, EligibleSnipe
from snipebot.config import Section
from snipebot.report import DigestTooLargeError, NameResolver, render_digest

from tests._helpers_digest import SEMESTER, TZ, report_of, roster_of

_BASE = 1_789_718_400  # 2026-09-18 08:00:00 UTC


def _snipe(i: int, sniper: str, target: str, groups: dict) -> EligibleSnipe:
    sec = _BASE + i
    return EligibleSnipe(
        ts=f"{sec}.000000", ts_us=sec * 1_000_000,
        date="2026-09-18", time="08:00:00",
        sniper=sniper, target=target,
        sniper_group=groups.get(sniper), target_group=groups.get(target),
        selfie=False,
    )


def _render(report, snipes, groups, cache):
    roster = roster_of(groups)
    elig = Eligibility(semester=SEMESTER, snipes=tuple(snipes), rejections=())
    return render_digest(
        report=report, period_key="daily:2026-09-18", semester=SEMESTER,
        elig=elig, roster=roster, opted_out=frozenset(),
        names=NameResolver(cache, roster), tz=TZ, revision=0, selfie_emoji="selfie",
    )


def _section_text(render, title: str) -> str:
    for b in render.blocks:
        if b.get("type") == "section" and b["text"]["text"].startswith(title):
            return b["text"]["text"]
    raise AssertionError(f"no section starting {title!r}")


def _name80(label: str) -> str:
    """An 80-character display name (Slack's display-name maximum)."""
    return (label + " " + "a" * 80)[:80]


# --------------------------------------------------------------------------- #
# 1. Head rows silently trimmed and mislabelled as a boundary tie
# --------------------------------------------------------------------------- #

def test_head_rows_never_collapsed_into_a_false_tie_line():
    """Claim: when the head rows (ranks 1..top_n) do not fit MAX_SECTION_TEXT,
    `_ranked_block` silently drops head rows and appends
    "...and +K others tied at V" although the dropped rows are NOT tied (here every
    row has a distinct count and len(rows) == top_n, so there is no boundary tie at
    all). Violates 30 section 3 ("head = rows[0:top_n] (ranks 1..top_n, always
    shown)"; "+K others refers only to the boundary tie") and 30 section 5.5 ("Only
    boundary-tie rows are ever trimmed ... A violated assertion is a bug, surfaced as
    DigestTooLargeError, never a silently truncated digest"). Reachable with config
    top_n = 20 (the allowed maximum) and 80-character display names: a most_sniped
    line carries two names (~180 chars), so 20 lines exceed 3000.
    """
    sniper = "U0AAA001"
    groups = {sniper: "sibA"}
    cache = {sniper: _name80("user-1")}
    snipes = []
    i = 0
    for n in range(20):
        target = f"U0AAA{300 + n}"
        groups[target] = "sibB"
        cache[target] = _name80(f"user-{300 + n}")
        for _ in range(n + 1):  # target n is sniped n+1 times: every count distinct
            snipes.append(_snipe(i, sniper, target, groups))
            i += 1
    report = report_of((Section.DAY, Section.MOST_SNIPED), top_n=20)

    try:
        render = _render(report, snipes, groups, cache)
    except DigestTooLargeError:
        return  # an explicit, spec-sanctioned failure is acceptable
    text = _section_text(render, "*Most sniped*")
    assert "others tied at" not in text, (
        "no two rows share a count and len(rows) == top_n, yet the section claims a "
        f"tie collapse: {text.rsplit(chr(10), 1)[-1]!r}"
    )
    missing = [t for t in (f"U0AAA{300 + n}" for n in range(20))
               if cache[t][:37] + "..." not in text]  # clipped, E-W4-27
    assert not missing, f"{len(missing)} head rows were silently dropped"


# --------------------------------------------------------------------------- #
# 3. Bare @channel / @here / @everyone in a display name reaches parsed mrkdwn
# --------------------------------------------------------------------------- #

_BROADCAST = re.compile(r"(?<![\w&])@(here|channel|everyone)\b")


def test_broadcast_words_in_names_are_not_left_to_mention_parsing():
    """Claim: names are only &/</> escaped, and the section text objects omit
    `"verbatim": true`, so a display name containing a bare `@channel`, `@here` or
    `@everyone` is sent in a mrkdwn text object that Slack preprocesses (Block Kit
    text object: with verbatim false, the default, "certain mentions will be
    automatically parsed"). Violates PLAN section 4 / 30 section 4 ("Standings print
    plain display names ... a digest must ping nobody"). Correct code either marks
    name-carrying mrkdwn text `verbatim: true` or neutralises the broadcast word.
    """
    victim = "U0AAA002"
    snipers = {"U0AAA001": "user-1 @channel", "U0AAA003": "@here user-3",
               "U0AAA004": "user-4 @everyone"}
    groups = {victim: "sibB", **{s: "sibA" for s in snipers}}
    cache = {victim: "user-2", **snipers}
    snipes = [_snipe(i, s, victim, groups) for i, s in enumerate(snipers)]
    report = report_of((Section.DAY, Section.TOP_SNIPERS, Section.MOST_SNIPED,
                        Section.PAIRS), top_n=5)

    render = _render(report, snipes, groups, cache)
    exposed = []
    for b in render.blocks:
        if b.get("type") != "section":
            continue
        objs = [b["text"], *b.get("fields", [])]
        for o in objs:
            if o.get("type") == "mrkdwn" and o.get("verbatim") is not True:
                exposed += _BROADCAST.findall(o["text"])
    assert not exposed, (
        f"broadcast words sent to Slack mention parsing: {sorted(set(exposed))}"
    )
