"""Round 3 red-team tests: invariant / interaction breaks in snipebot/parse.py.

Each test is a proof that the shipped code violates a stated rule in the spec.
Inputs are built by hand; every test here is expected to FAIL against the
current code (a passing test would not be a finding).
"""

from __future__ import annotations

import warnings

from snipebot.parse import parse

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"


def _msg(text: str, **kw: object) -> dict:
    m: dict = {
        "type": "message",
        "user": "U0AAA001",
        "ts": "1758210000.000500",
        "subtype": "file_share",
        "text": text,
        "files": [
            {
                "id": "F0FILE001",
                "mimetype": "image/jpeg",
                "name": "photo-1.jpg",
                "size": 100000,
                "original_w": 1000,
                "original_h": 1000,
            }
        ],
    }
    m.update(kw)
    return m


def _parse(m: dict) -> object:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return parse(m, CHANNEL, BOT)


def test_inline_code_span_stripped_across_newline_drops_pinged_mention() -> None:
    """A single-backtick span that appears to span a newline must not swallow a
    mention that Slack pings.

    spec 10-slack-io.md section 4 ("Mention extraction", DECISION):
    "mentions inside a blockquote are included; mentions inside code are
    excluded. Reason: this matches Slack's real ping behaviour ... so the text
    and blocks paths agree."

    A Slack single-backtick inline code span does not span a line break (only a
    triple-backtick fence spans lines); the opening backtick is therefore
    literal and `<@U0AAA002>` on the following line is a real, pinged mention.
    The shipped inline stripper `_INLINE_RE = re.compile(r"`[^`]*`")` matches
    `[^`]` against the newline, so it deletes the whole `\\`a\\n<@U0AAA002>\\``
    span and the pinged mention is dropped from `targets`, contradicting
    "matches Slack's real ping behaviour".
    """
    cand = _parse(_msg("start `a\n<@U0AAA002>` end"))
    assert "U0AAA002" in cand.targets
