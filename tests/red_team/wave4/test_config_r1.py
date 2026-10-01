"""Wave 4, round 1 red-team: config.yaml parsing and validation.

Each test writes a raw YAML document under tmp_path and drives it through
``load_config``. A test asserts the specification-mandated behaviour; it is a
finding only while it FAILS against the current code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snipebot.config import ConfigError, InvalidValueError, load_config

_HEAD = """\
slack: {channel: C0MAIN01}
timezone: America/New_York
rules: {}
consent: {veto: {emoji: x}}
feedback: {reactions: {}}
"""

_SEMESTERS = "semesters: [{name: fall, start: 2026-09-01, end: 2026-12-20}]\n"
_PLAYERS = "players: {extras: [U0AAA001]}\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_duplicate_mapping_key_silently_drops_a_group(tmp_path: Path) -> None:
    """40-config-cli.md section 1 DECISION ("a typo ... must fail loudly at config
    load, not silently mis-route at runtime") and PLAN section 3 ("Validation fails
    loudly on ... a user in two groups"): a YAML mapping that repeats a key is a
    typo that must raise a ConfigError. yaml.safe_load keeps only the LAST value,
    so a second ``reds:`` group silently replaces the first and U0AAA001 vanishes
    from the roster (every snipe by or of them is then judged off-roster: wrong
    scores, no error). The same last-wins loss applies to a repeated top-level
    section such as ``players:``.
    """
    group_dup = (
        _HEAD + _SEMESTERS
        + "players:\n  groups:\n    reds: [U0AAA001]\n    reds: [U0AAA002]\n"
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, group_dup))

    top_dup = _HEAD + _SEMESTERS + _PLAYERS + "players: {extras: [U0AAA002]}\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, top_dup))


def test_impossible_bare_yaml_date_escapes_as_plain_valueerror(tmp_path: Path) -> None:
    """40-config-cli.md section 2.1: load_config "Raises a subclass of ConfigError
    (section 2.2) on any invalid input"; section 1 table: a bad ``Date``/``DateTime``
    is ``InvalidValueError``. A bare YAML date that is not a calendar date
    (``2026-02-30``) or a bare timestamp with an impossible hour (``25:00:00``) makes
    PyYAML's constructor raise a plain ValueError, which is not a yaml.YAMLError, so
    load_config lets it escape uncaught. The CLI then exits UNEXPECTED instead of 2
    (config invalid) with "day is out of range for month" and no key path.
    """
    bad_date = (
        _HEAD + "semesters: [{name: fall, start: 2026-02-30, end: 2026-12-20}]\n"
        + _PLAYERS
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad_date))

    bad_stamp = (
        _HEAD + _SEMESTERS
        + "players:\n  extras:\n    - id: U0AAA001\n      from: 2026-09-15 25:00:00\n"
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad_stamp))


def test_timezone_naming_a_zone_directory_escapes_as_oserror(tmp_path: Path) -> None:
    """40-config-cli.md section 1 table: ``Tz`` is "an IANA name accepted by
    zoneinfo.ZoneInfo", violation -> ``InvalidValueError``; section 2.1: any invalid
    input raises a ConfigError. ``timezone: America`` (a region prefix, i.e. a
    directory inside the tzdata package) makes ZoneInfo raise an OSError
    (PermissionError on Windows, IsADirectoryError elsewhere), which load_config does
    not catch. The CLI then exits UNEXPECTED and prints the OSError text, which holds
    the full filesystem path of the Python install (40 section 4: logs carry IDs
    only, never paths).
    """
    text = _HEAD.replace("America/New_York", "America") + _SEMESTERS + _PLAYERS
    with pytest.raises(InvalidValueError) as excinfo:
        load_config(_write(tmp_path, text))
    assert "timezone" in str(excinfo.value)


def test_far_calendar_edge_semester_overflows(tmp_path: Path) -> None:
    """40-config-cli.md section 2.1: load_config either returns a resolved Config or
    raises a ConfigError; section 1.4: ``end`` is any ``Date`` (YYYY-MM-DD).
    ``end: 9999-12-31`` (an owner's "open-ended" semester) is a well-formed Date, but
    _local_to_us adds the zone offset past datetime.max and raises OverflowError,
    which is neither a Config nor a ConfigError (CLI exit UNEXPECTED, no key path).
    The same happens for ``start: 0001-01-01`` in a zone east of UTC.
    """
    text = _HEAD + "semesters: [{name: fall, start: 2026-09-01, end: 9999-12-31}]\n" + _PLAYERS
    try:
        load_config(_write(tmp_path, text))
    except ConfigError:
        pass


@pytest.mark.parametrize(
    "fragment",
    [
        "sync: null\n",
        "consent:\n  veto: {emoji: x}\n  optout_messages: null\n",
        "consent:\n  veto: {emoji: x}\n  opted_out: null\n",
    ],
)
def test_null_section_or_list_coerced_to_default(tmp_path: Path, fragment: str) -> None:
    """40-config-cli.md section 1: "A key with the wrong scalar type raises
    InvalidValueError"; section 1.1 types ``sync`` as a mapping and section 1.7 types
    ``consent.optout_messages`` / ``consent.opted_out`` as lists. The earlier rulings
    (wave 1: ``faces: null`` and ``reports: null`` must raise) bind every key
    uniformly, but ``sync: null`` is still coerced to all defaults (_resolve_sync
    ``if raw is None: raw = {}``) and a null consent list to ``[]`` (_optional_list),
    so a section whose body was lost to an indentation slip loads silently.
    """
    head = _HEAD
    if fragment.startswith("consent:"):
        head = head.replace("consent: {veto: {emoji: x}}\n", "")
    text = head + _SEMESTERS + _PLAYERS + fragment
    with pytest.raises(InvalidValueError):
        load_config(_write(tmp_path, text))


def test_horizon_omitted_with_long_scan_does_not_say_set_explicitly(tmp_path: Path) -> None:
    """40-config-cli.md section 1.3: "When the key is omitted and scan_days exceeds
    80, load fails with InvalidValueError naming sync.history_horizon_days and saying
    it must be set explicitly (null or >= scan_days)". The message today is
    "sync.history_horizon_days: must be null or >= scan_days": it never tells the
    owner, who did not write the key at all, that the key has to be added.
    """
    text = _HEAD + _SEMESTERS + _PLAYERS + "sync: {scan_days: 90}\n"
    with pytest.raises(InvalidValueError) as excinfo:
        load_config(_write(tmp_path, text))
    message = str(excinfo.value)
    assert "sync.history_horizon_days" in message
    assert "explicit" in message
