"""Regression tests for ruling E-W4-39: the `recaps` switch (40 §1.1, §4.2, §7.2; 20 §6).

`recaps: false` stops step 9 (no digest posted or revised, for every command) and leaves
reactions, the ledger and its commit alone. It is not an evaluate input, so it enters no
fingerprint. `snipebot recaps [on|off]` prints or flips it by editing config.yaml as text,
and the admin workflow's `recaps-on` / `recaps-off` choices run that command and commit the
file like a rules bump.

Offline only: FakeSlack worlds, files persistence under tmp_path, and git only against a bare
origin plus a clone under tmp_path.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.config import (
    Config,
    ConfigError,
    InvalidValueError,
    RecapsValueError,
    compute_fingerprints,
    fingerprint_guard,
    load_config,
)
from snipebot.faces import FakeFaceDetector
from snipebot.sync import Command, SyncResult, run_sync
from snipebot.ts import parse_ts

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import data_paths, image_file, make_config, mkts, roster_of
from tests.red_team.wave4.test_deploy_r2 import _admin_argv, _git_bash

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
ADMIN = "U0AAA009"

# A hand-written config whose comments and layout the CLI must keep byte for byte.
BASE = """\
# club config: comments and spacing here must survive a recaps edit
enabled: true                 # kill switch
persistence: files            # temp-file rename

slack:
  channel: C0MAIN01           # the watched channel

timezone: UTC

semesters:
  - name: fall-2026
    start: 2026-09-01
    end: 2026-12-20

rules:
  cooldown:
    minutes: 15

players:
  groups:
    reds:
      - U0AAA001
      - U0AAA002

consent:
  veto:
    emoji: x

admins: [U0AAA009]

feedback: {}

reports:
  - name: daily
    every: 1d
    at: "11:00"
    sections: [day]
"""


def _write(tmp_path: Path, text: str, *, name: str = "config.yaml", newline: str = "\n") -> Path:
    path = tmp_path / name
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _with(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


# --- config: parsing and validation ----------------------------------------------------------

def test_absent_key_defaults_to_on(tmp_path):
    assert load_config(_write(tmp_path, BASE)).recaps is True


@pytest.mark.parametrize("written, expected", [
    ("true", True), ("false", False), ("True", True), ("FALSE", False),
])
def test_plain_booleans_load(tmp_path, written, expected):
    text = _with(BASE, "persistence:", f"recaps: {written}\npersistence:")
    assert load_config(_write(tmp_path, text)).recaps is expected


@pytest.mark.parametrize("written", [
    "yes", "no", "on", "off", "Yes", "OFF", '"true"', "'false'", "1", "0", "null", "~",
    "[]", "{}", "enabled", "!!bool yes",
])
def test_anything_but_true_or_false_is_refused_naming_the_key(tmp_path, written):
    text = _with(BASE, "persistence:", f"recaps: {written}\npersistence:")
    with pytest.raises(RecapsValueError) as info:
        load_config(_write(tmp_path, text))
    assert isinstance(info.value, InvalidValueError)
    assert isinstance(info.value, ConfigError)
    assert str(info.value).startswith("recaps:")


def test_empty_value_is_refused(tmp_path):
    text = _with(BASE, "persistence:", "recaps:\npersistence:")
    with pytest.raises(RecapsValueError):
        load_config(_write(tmp_path, text))


def test_nested_recaps_key_is_still_unknown(tmp_path):
    """Only the top-level key exists; `sync.recaps` stays an unknown key."""
    text = BASE + "sync:\n  recaps: false\n"
    with pytest.raises(ConfigError, match=r"sync\.recaps"):
        load_config(_write(tmp_path, text))


def test_config_constructed_without_recaps_defaults_on():
    cfg = make_config(roster=roster_of({SNIPER: "reds"}))
    assert isinstance(cfg, Config) and cfg.recaps is True


def test_recaps_enters_no_fingerprint_and_never_trips_the_guard(tmp_path):
    on = load_config(_write(tmp_path, BASE, name="on.yaml"))
    off = load_config(_write(
        tmp_path, _with(BASE, "persistence:", "recaps: false\npersistence:"), name="off.yaml"))
    assert on.recaps is True and off.recaps is False
    h_us = parse_ts(mkts(2026, 9, 18, 10))
    stored = compute_fingerprints(on, h_us)
    assert compute_fingerprints(off, h_us) == stored
    assert compute_fingerprints(off, None) == compute_fingerprints(on, None)
    fingerprint_guard(off, [h_us], stored, h_us)          # no FingerprintGuardError
    fingerprint_guard(on, [h_us], compute_fingerprints(off, h_us), h_us)


# --- sync: step 9 gated -----------------------------------------------------------------------

class _CountingSlack(FakeSlack):
    """FakeSlack that records every chat.postMessage / chat.update call."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.posted: list[str] = []
        self.updated: list[str] = []

    def post_message(self, channel, **kw):
        self.posted.append(channel)
        return super().post_message(channel, **kw)

    def update_message(self, channel, ts, **kw):
        self.updated.append(channel)
        return super().update_message(channel, ts, **kw)


SNIPE_TS = mkts(2026, 9, 18, 12)
NOW = mkts(2026, 9, 18, 21, 30)                 # 30 min past make_config's 21:00 daily anchor


def _world(now: str = NOW) -> _CountingSlack:
    users = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in (SNIPER, TARGET, ADMIN):
        users[uid] = FakeUser(id=uid, display_name=uid)
    slack = _CountingSlack(now=now, bot_user_id=BOT, users=users)
    slack.post(at=SNIPE_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[image_file("F0FILE001", b"snap1", name="photo-1.png")])
    return slack


def _config(*, recaps: bool) -> Config:
    cfg = make_config(roster=roster_of({SNIPER: "reds", TARGET: "reds", ADMIN: None}),
                      admins=(ADMIN,))
    assert cfg.channel == CHANNEL
    return dataclasses.replace(cfg, recaps=recaps)


def _sync(slack, cfg, root: Path, *, now: str = NOW, **kw) -> SyncResult:
    root.mkdir(parents=True, exist_ok=True)
    led, st = data_paths(root)
    return run_sync(slack, cfg, detector=FakeFaceDetector({}), ledger_path=led,
                    state_path=st, now_us=parse_ts(now), **kw)


def _bot_reactions(slack) -> list[tuple[str, str, str]]:
    return [(e["kind"], e["ts"], e["data"]["name"]) for e in slack._events
            if e["kind"] in ("react", "unreact") and e["actor"] == BOT]


def test_recaps_off_posts_nothing_but_reacts_and_writes_the_ledger(tmp_path, capsys):
    slack = _world()
    result = _sync(slack, _config(recaps=False), tmp_path / "off")
    assert result.exit_code == 0
    assert slack.posted == [] and slack.updated == []
    assert result.digests_posted == 0 and result.digests_revised == 0
    assert ("react", SNIPE_TS, "white_check_mark") in _bot_reactions(slack)
    assert result.ledger_written
    led, _ = data_paths(tmp_path / "off")
    assert SNIPE_TS in led.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    skipped = [ln for ln in err.splitlines() if "digests skipped" in ln]
    assert len(skipped) == 1
    assert "INFO" in skipped[0] and "reason=recaps_off" in skipped[0]


def test_recaps_on_positive_control_and_identical_ledger(tmp_path, capsys):
    """The same world with recaps on posts the digest; everything else is byte-identical."""
    off_slack, on_slack = _world(), _world()
    _sync(off_slack, _config(recaps=False), tmp_path / "off")
    capsys.readouterr()
    on = _sync(on_slack, _config(recaps=True), tmp_path / "on")
    assert on.digests_posted == 1 and on_slack.posted == [CHANNEL]
    assert "digests skipped" not in capsys.readouterr().err
    assert _bot_reactions(off_slack) == _bot_reactions(on_slack)
    for name in ("ledger.jsonl", "verdicts.jsonl"):
        assert ((tmp_path / "off" / "data" / name).read_bytes()
                == (tmp_path / "on" / "data" / name).read_bytes())


def test_recaps_off_does_not_revise_a_posted_digest(tmp_path):
    slack = _world()
    first = _sync(slack, _config(recaps=True), tmp_path)
    assert first.digests_posted == 1
    # A second counted snipe changes the numbers, which would be a Pass B revision.
    slack.post(at=mkts(2026, 9, 18, 13), user=TARGET, channel=CHANNEL, text=f"<@{SNIPER}>",
               files=[image_file("F0FILE002", b"snap2", name="photo-2.png")])
    later = mkts(2026, 9, 18, 21, 50)
    slack.as_of(later)
    second = _sync(slack, _config(recaps=False), tmp_path, now=later)
    assert second.exit_code == 0
    assert second.digests_posted == 0 and second.digests_revised == 0
    assert slack.updated == [] and slack.posted == [CHANNEL]
    # Flipping it back on revises the digest on the next run (the switch is not sticky).
    slack.as_of(mkts(2026, 9, 18, 22))
    third = _sync(slack, _config(recaps=True), tmp_path, now=mkts(2026, 9, 18, 22))
    assert third.digests_revised == 1 and slack.updated == [CHANNEL]


def test_admin_veto_honours_recaps_off(tmp_path):
    slack = _world()
    _sync(slack, _config(recaps=False), tmp_path, no_post=False)
    later = mkts(2026, 9, 18, 21, 40)
    slack.as_of(later)
    result = _sync(slack, _config(recaps=False), tmp_path, now=later,
                   command=Command.VETO, veto_ts=SNIPE_TS, veto_by=ADMIN)
    assert result.exit_code == 0
    assert slack.posted == [] and slack.updated == []


def test_no_post_with_recaps_off_logs_no_skip_line(tmp_path, capsys):
    slack = _world()
    result = _sync(slack, _config(recaps=False), tmp_path, no_post=True)
    assert result.exit_code == 0 and slack.posted == []
    assert "recaps_off" not in capsys.readouterr().err


def test_cli_sync_with_recaps_false_in_config_posts_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    now = mkts(2026, 9, 18, 12)
    monkeypatch.setattr(cli, "_now_us", lambda: parse_ts(now))
    cfg = _write(tmp_path, _with(BASE, "persistence:", "recaps: false\npersistence:"))
    slack = _world(now)
    slack.post(at=mkts(2026, 9, 18, 10), user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}>",
               files=[image_file("F0FILE003", b"snap3", name="photo-3.png")])
    data = tmp_path / "data"
    code = main(["--config", str(cfg), "--data-dir", str(data), "sync"],
                slack_factory=lambda: slack, detector_factory=lambda: FakeFaceDetector({}))
    assert code == 0
    assert slack.posted == [] and slack.updated == []
    assert (data / "ledger.jsonl").exists()
    # Positive control: the same config with recaps on posts the due 11:00 daily digest.
    cfg.write_text(BASE, encoding="utf-8")
    assert main(["--config", str(cfg), "--data-dir", str(data), "sync"],
                slack_factory=lambda: slack,
                detector_factory=lambda: FakeFaceDetector({})) == 0
    assert slack.posted == [CHANNEL]


# --- CLI: `snipebot recaps [on|off]` ---------------------------------------------------------

def _recaps(cfg: Path, *state: str) -> int:
    return main(["--config", str(cfg), "recaps", *state])


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)


def test_status_prints_on_for_an_absent_key(tmp_path, capsys):
    cfg = _write(tmp_path, BASE)
    assert _recaps(cfg) == Exit.OK
    assert capsys.readouterr().out == "recaps: on\n"
    assert cfg.read_text(encoding="utf-8") == BASE


def test_status_prints_off(tmp_path, capsys):
    cfg = _write(tmp_path, _with(BASE, "persistence:", "recaps: false\npersistence:"))
    assert _recaps(cfg) == Exit.OK
    assert capsys.readouterr().out == "recaps: off\n"


def test_status_on_an_invalid_config_exits_config_invalid(tmp_path, capsys):
    cfg = _write(tmp_path, _with(BASE, "persistence:", "recaps: maybe\npersistence:"))
    assert _recaps(cfg) == Exit.CONFIG_INVALID
    assert "recaps" in capsys.readouterr().err


def test_off_inserts_right_after_the_enabled_line(tmp_path, capsys):
    cfg = _write(tmp_path, BASE)
    assert _recaps(cfg, "off") == Exit.OK
    assert capsys.readouterr().out == "recaps: off\n"
    expected = _with(BASE, "# kill switch\n", "# kill switch\nrecaps: false\n")
    assert cfg.read_text(encoding="utf-8") == expected
    assert load_config(cfg).recaps is False


def test_off_without_an_enabled_line_inserts_at_the_top(tmp_path):
    text = _with(BASE, "enabled: true                 # kill switch\n", "")
    cfg = _write(tmp_path, text)
    assert _recaps(cfg, "off") == Exit.OK
    assert cfg.read_text(encoding="utf-8") == "recaps: false\n" + text


def test_indented_enabled_or_recaps_lines_are_not_top_level(tmp_path):
    text = BASE.replace("enabled: true                 # kill switch\n", "") + (
        "faces:\n  max_attempts: 3   # recaps: true\n")
    cfg = _write(tmp_path, text)
    assert _recaps(cfg, "off") == Exit.OK
    assert cfg.read_text(encoding="utf-8") == "recaps: false\n" + text


def test_existing_line_value_replaced_and_comment_kept(tmp_path, capsys):
    text = _with(BASE, "persistence:", "recaps:   true    # club asked 2026-09-30\npersistence:")
    cfg = _write(tmp_path, text)
    assert _recaps(cfg, "off") == Exit.OK
    assert cfg.read_text(encoding="utf-8") == _with(
        text, "recaps:   true    #", "recaps:   false    #")
    assert _recaps(cfg, "on") == Exit.OK
    assert cfg.read_text(encoding="utf-8") == text
    assert capsys.readouterr().out == "recaps: off\nrecaps: on\n"


def test_on_over_false_rewrites_only_the_value(tmp_path):
    text = _with(BASE, "persistence:", "recaps: false\npersistence:")
    cfg = _write(tmp_path, text)
    assert _recaps(cfg, "on") == Exit.OK
    assert cfg.read_bytes() == _with(text, "recaps: false", "recaps: true").encode("utf-8")
    assert load_config(cfg).recaps is True


def test_setting_the_current_state_changes_no_byte(tmp_path, capsys):
    cfg = _write(tmp_path, BASE)
    assert _recaps(cfg, "on") == Exit.OK             # absent already reads on
    assert cfg.read_text(encoding="utf-8") == BASE
    assert _recaps(cfg, "off") == Exit.OK
    once = cfg.read_bytes()
    assert _recaps(cfg, "off") == Exit.OK
    assert cfg.read_bytes() == once
    upper = _with(BASE, "persistence:", "recaps: False  # x\npersistence:")
    cfg.write_text(upper, encoding="utf-8")
    assert _recaps(cfg, "off") == Exit.OK
    assert cfg.read_text(encoding="utf-8") == upper
    assert capsys.readouterr().out == "recaps: on\nrecaps: off\nrecaps: off\nrecaps: off\n"


def test_crlf_and_bom_are_preserved(tmp_path):
    cfg = tmp_path / "config.yaml"
    raw = b"\xef\xbb\xbf" + BASE.replace("\n", "\r\n").encode("utf-8")
    cfg.write_bytes(raw)
    assert _recaps(cfg, "off") == Exit.OK
    out = cfg.read_bytes()
    assert out == raw.replace(b"# kill switch\r\n", b"# kill switch\r\nrecaps: false\r\n")
    assert load_config(cfg).recaps is False


def test_enabled_as_the_last_line_without_a_newline(tmp_path):
    text = BASE.replace("enabled: true                 # kill switch\n", "") + "enabled: true"
    cfg = _write(tmp_path, text)
    assert _recaps(cfg, "off") == Exit.OK
    assert cfg.read_text(encoding="utf-8") == text + "\nrecaps: false"
    assert load_config(cfg).recaps is False


def test_an_invalid_result_is_rolled_back_byte_for_byte(tmp_path, capsys):
    bad = _with(BASE, "timezone: UTC", "timezone: Nowhere/Nothing")
    cfg = _write(tmp_path, bad)
    before = cfg.read_bytes()
    assert _recaps(cfg, "off") == Exit.CONFIG_INVALID
    assert cfg.read_bytes() == before
    assert "timezone" in capsys.readouterr().err


def test_a_line_that_does_not_take_effect_is_rolled_back(tmp_path):
    """`recaps:` followed by a continuation line would read `false true` (a string)."""
    text = _with(BASE, "persistence:", "recaps:\n  true\npersistence:")
    cfg = _write(tmp_path, text)
    assert _recaps(cfg, "off") == Exit.CONFIG_INVALID
    assert cfg.read_text(encoding="utf-8") == text


def test_a_refused_value_is_fixed_by_setting_a_state(tmp_path):
    cfg = _write(tmp_path, _with(BASE, "persistence:", "recaps: yes  # typo\npersistence:"))
    assert _recaps(cfg, "off") == Exit.OK
    assert "recaps: false  # typo\n" in cfg.read_text(encoding="utf-8")
    assert load_config(cfg).recaps is False


def test_missing_config_exits_config_invalid(tmp_path):
    assert _recaps(tmp_path / "absent.yaml", "off") == Exit.CONFIG_INVALID
    assert not (tmp_path / "absent.yaml").exists()


def test_bad_state_is_an_argument_error(tmp_path):
    cfg = _write(tmp_path, BASE)
    with pytest.raises(SystemExit) as info:
        _recaps(cfg, "maybe")
    assert info.value.code == 2
    assert cfg.read_text(encoding="utf-8") == BASE


def test_recaps_never_builds_a_slack_client_or_touches_the_data_dir(tmp_path):
    cfg = _write(tmp_path, BASE)
    data = tmp_path / "data"

    def _no_slack():
        raise AssertionError("recaps built a Slack client")

    assert main(["--config", str(cfg), "--data-dir", str(data), "recaps", "off"],
                slack_factory=_no_slack) == Exit.OK
    assert not data.exists()


# --- admin.yml --------------------------------------------------------------------------------

def _admin() -> dict:
    return yaml.safe_load((WORKFLOWS / "admin.yml").read_text(encoding="utf-8"))


def _step(name: str) -> dict:
    return next(s for s in _admin()["jobs"]["admin"]["steps"] if s.get("name") == name)


def test_admin_offers_recaps_choices():
    options = _admin()[True]["workflow_dispatch"]["inputs"]["command"]["options"]
    assert "recaps-on" in options and "recaps-off" in options


def test_admin_recaps_commands_run_the_cli_without_a_data_dir():
    script = _step("Run admin command")["run"]
    lines = {ln.strip().split(")", 1)[0]: ln for ln in script.splitlines()
             if ln.strip().startswith(("recaps-on)", "recaps-off)"))}
    assert "python -m snipebot recaps on ;;" in lines["recaps-on"]
    assert "python -m snipebot recaps off ;;" in lines["recaps-off"]
    assert "$D" not in lines["recaps-on"] and "$D" not in lines["recaps-off"]


@pytest.mark.parametrize("command, argv", [
    ("recaps-on", ["recaps", "on"]), ("recaps-off", ["recaps", "off"]),
])
def test_admin_recaps_script_invocation(tmp_path, command, argv):
    assert _admin_argv({"command": command}, tmp_path) == argv


def test_admin_commit_step_covers_recaps_and_rules_bump():
    step = _step("Commit config change")
    cond = step["if"]
    for command in ("rules-bump", "recaps-on", "recaps-off"):
        assert f"inputs.command == '{command}'" in cond
    assert step["env"]["COMMAND"] == "${{ inputs.command }}"
    script = step["run"]
    assert "${{ inputs." not in script
    assert 'recaps-on)  MSG="recaps on"' in script
    assert 'recaps-off) MSG="recaps off"' in script
    assert 'MSG="rules bump effective $EFFECTIVE_FROM"' in script
    assert "git diff --cached --quiet" in script
    assert script.index("git diff --cached --quiet") < script.index("git commit")


def _run(args, cwd, env=None):
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def test_admin_commit_step_commits_once_and_skips_a_no_op(tmp_path):
    """The commit step, run under bash in a clone of a tmp origin: `recaps off` lands one
    commit named `recaps off`; a second `recaps off` stages nothing and the step exits 0."""
    bash = _git_bash()
    if bash is None:
        pytest.skip("no bash to execute the workflow script")
    gitcfg = tmp_path / "gitconfig"
    gitcfg.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLACK_", "GIT_", "SNIPEBOT_"))}
    env.update(GIT_CONFIG_GLOBAL=str(gitcfg), GIT_CONFIG_NOSYSTEM="1")
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path, env)
    clone = tmp_path / "clone"
    _run(["git", "clone", str(origin), str(clone)], tmp_path, env)
    (clone / "config.yaml").write_text(BASE, encoding="utf-8", newline="\n")
    ident = ["-c", "user.name=seed", "-c", "user.email=seed@fixture.invalid",
             "-c", "core.autocrlf=false"]
    _run(["git", *ident, "add", "config.yaml"], clone, env)
    _run(["git", *ident, "commit", "-m", "seed"], clone, env)
    _run(["git", *ident, "push", "-u", "origin", "main"], clone, env)

    script = tmp_path / "commit.sh"
    script.write_text(_step("Commit config change")["run"], encoding="utf-8", newline="\n")
    step_env = dict(env, COMMAND="recaps-off", EFFECTIVE_FROM="")

    assert _recaps(clone / "config.yaml", "off") == Exit.OK
    _run([bash, str(script)], clone, step_env)
    assert _run(["git", "log", "--format=%s", "main"], origin, env).splitlines() == [
        "recaps off", "seed"]

    assert _recaps(clone / "config.yaml", "off") == Exit.OK
    out = _run([bash, str(script)], clone, step_env)
    assert "unchanged" in out
    assert _run(["git", "log", "--format=%s", "main"], origin, env).splitlines() == [
        "recaps off", "seed"]
