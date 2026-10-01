"""Privacy of what is stored, printed and written to disk (round 2): the erased user after
`purge --rewrite-history`, and image bytes on the face-count path.

Git tests build a bare `origin` whose `data` branch has one seed commit, clone it as the
data checkout (`SNIPEBOT_DATA_REPO`), and drive the CLI end to end against a `FakeSlack`
world under `persistence: git`. Real git runs only inside `tmp_path`.
"""

from __future__ import annotations

import calendar
import subprocess
from pathlib import Path

import pytest
import yaml

import snipebot
from snipebot import cli
from snipebot.cli import Exit, main
from snipebot.faces import FakeFaceDetector
from snipebot.ts import US_PER_SECOND

from tests.fake_slack import FakeSlack, FakeUser

CHANNEL = "C0MAIN01"
BOT = "U0BOT01"
ADMIN = "U0AAA009"
SNIPER = "U0AAA001"
TARGET = "U0AAA002"
OTHER = "U0AAA003"


def _secs(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def _ts(y, mo, d, h=0, mi=0, s=0) -> str:
    return f"{_secs(y, mo, d, h, mi, s)}.000000"


NOW_US = _secs(2026, 9, 18, 12) * US_PER_SECOND
MSG_TS = _ts(2026, 9, 18, 9)


# --------------------------------------------------------------------------- helpers

def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """(origin, checkout): a bare origin with a seeded `data` branch and its clone."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "data", str(origin)])
    seed = tmp_path / "seed"
    _run(["git", "clone", str(origin), str(seed)])
    (seed / "data").mkdir(exist_ok=True)
    (seed / "data" / "README").write_text("data branch\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@fixture.invalid",
         "commit", "-m", "init data branch")
    _git(seed, "branch", "-M", "data")
    _git(seed, "push", "-u", "origin", "data")
    repo = tmp_path / "repo"
    _run(["git", "clone", str(origin), str(repo)])
    _git(repo, "checkout", "data")
    return origin, repo


def _config(tmp_path: Path) -> Path:
    doc = {
        "enabled": True,
        "persistence": "git",
        "slack": {"channel": CHANNEL},
        "timezone": "UTC",
        "sync": {
            "interval_minutes": 10, "scan_days": 14, "history_horizon_days": 90,
            "max_deletes_per_run": 5, "large_movement_rows": 25,
        },
        "semesters": [{"name": "fall-2026", "start": "2026-09-01", "end": "2026-12-20"}],
        "rules": {
            "cooldown": {"minutes": 15, "scope": "pair", "rejected_attempts_reset": False},
            "multi_tag": "per_target",
            "max_targets_per_message": None,
            "edit_grace_minutes": 10,
            "max_snipes_per_target_per_day": None,
            "allow_self": False,
            "allow_bots": False,
            "count_thread_replies": False,
            "count_image_links": False,
            "allow_video": False,
            "selfie_bonus": False,
        },
        "players": {"count_intra_group": True,
                    "groups": {"fam": [SNIPER, TARGET, OTHER]}, "extras": []},
        "consent": {
            "veto": {"emoji": "no_entry_sign", "by": ["admins"]},
            "optout_messages": [],
            "opted_out": [],
        },
        "admins": [ADMIN],
        "feedback": {
            "reactions": {
                "counted": "white_check_mark",
                "cooldown": "hourglass_flowing_sand",
                "untagged": None,
                "not_counted": "x",
                "selfie": None,
            },
            "review": {"min_targets": 5, "emoji": "question"},
        },
        "reports": [],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _photo(file_id: str, data: bytes) -> dict:
    return {
        "id": file_id,
        "mimetype": "image/jpeg",
        "name": "photo-1.jpg",
        "title": "photo-1",
        "size": len(data),
        "original_w": 100,
        "original_h": 100,
        "thumb_1024": f"https://fixture.invalid/{file_id}/thumb_1024",
        "url_private": f"https://fixture.invalid/{file_id}/photo-1.jpg",
        "url_private_download": f"https://fixture.invalid/{file_id}/download/photo-1.jpg",
        "permalink": f"https://fixture.invalid/archives/{file_id}",
        "_bytes": data,
    }


def _world() -> FakeSlack:
    users = {
        BOT: FakeUser(id=BOT, is_bot=True),
        SNIPER: FakeUser(id=SNIPER, display_name="user-1", real_name="user-1"),
        TARGET: FakeUser(id=TARGET, display_name="user-2", real_name="user-2"),
        OTHER: FakeUser(id=OTHER, display_name="user-3", real_name="user-3"),
        ADMIN: FakeUser(id=ADMIN, display_name="user-9", real_name="user-9"),
    }
    return FakeSlack(now=_ts(2026, 9, 18, 12), bot_user_id=BOT, users=users)


def _all_commits(origin: Path) -> list[str]:
    return _git(origin, "rev-list", "data").split()


def _blobs_naming(origin: Path, user: str) -> list[tuple[str, str]]:
    found = []
    for sha in _all_commits(origin):
        for path in ("data/ledger.jsonl", "data/verdicts.jsonl"):
            proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=str(origin),
                                  capture_output=True, text=True)
            if proc.returncode == 0 and user in proc.stdout:
                found.append((sha[:8], path))
    return found


# --------------------------------------------------------------------------- findings

def test_purge_leaves_an_edited_out_target_in_first_seen_targets(tmp_path, monkeypatch, capsys):
    """40-config-cli.md §4.2 `purge` ("genuine erasure", plan §6/§8): removes every row where
    the user is sender or target from the live ledger and rewrites the data-branch history to
    drop them. A user tagged in a snipe that was later edited to drop that tag stays in the
    row's `first_seen_targets` (00-data §2), but `_cmd_purge` and `_rewrite_data_history`
    match only `sender`/`targets`. The erased user's ID therefore survives in the live
    ledger and in every rewritten snapshot pushed to the `data` branch."""
    origin, repo = _make_repo(tmp_path)
    monkeypatch.setenv("SNIPEBOT_DATA_REPO", str(repo))
    monkeypatch.delenv("SNIPEBOT_CRASH_AT", raising=False)
    clock = {"now": NOW_US}
    monkeypatch.setattr(cli, "_now_us", lambda: clock["now"])
    cfg = _config(tmp_path)
    slack = _world()
    slack.as_of(_ts(2026, 9, 18, 9, 30))
    clock["now"] = _secs(2026, 9, 18, 9, 30) * US_PER_SECOND
    slack.post(at=MSG_TS, user=SNIPER, channel=CHANNEL, text=f"<@{TARGET}> <@{OTHER}>",
               files=[_photo("F0FILE001", b"snap-bytes")])
    argv = ["--config", str(cfg), "--data-dir", str(repo / "data")]

    def cli_run(*args: str) -> int:
        return main([*args, *argv], slack_factory=lambda: slack,
                    detector_factory=lambda: FakeFaceDetector({}))

    assert cli_run("sync", "--no-react", "--no-post") == int(Exit.OK)
    # the tag on TARGET is edited away; OTHER stays tagged, so the row is kept
    slack.edit(at=_ts(2026, 9, 18, 10), ts=MSG_TS, channel=CHANNEL, user=SNIPER,
               text=f"<@{OTHER}>")
    slack.as_of(_ts(2026, 9, 18, 12))
    clock["now"] = NOW_US
    assert cli_run("sync", "--no-react", "--no-post") == int(Exit.OK)
    live = (repo / "data" / "ledger.jsonl").read_text(encoding="utf-8")
    assert TARGET in live            # sanity: first_seen_targets remembers the first tag
    capsys.readouterr()

    rc = cli_run("purge", "--user", TARGET, "--rewrite-history", "--yes")
    assert rc == int(Exit.OK)
    live = (repo / "data" / "ledger.jsonl").read_text(encoding="utf-8")
    assert TARGET not in live
    assert _blobs_naming(origin, TARGET) == []


def test_face_count_pil_fallback_writes_image_bytes_to_disk(monkeypatch):
    """PLAN.md §1a: the bot fetches each live image "into memory (never to disk ...)";
    faces.py `_decode` promises "Nothing is ever written to disk". The round-1 repair only
    keeps OpenCV away from formats it would spill; every other format goes to the PIL
    fallback, which opens any Pillow format. For EPS bytes (`%!PS-Adobe`), Pillow's
    EpsImagePlugin copies the whole buffer into a `tempfile.mkstemp()` file and runs
    Ghostscript on it (whenever `gs` is on PATH, as on common Linux runners), so the image
    bytes land on disk and an external interpreter runs over untrusted channel content.
    Reachable when Slack made no thumbnail for an image upload and the fetch falls back to
    the original `url_private_download` bytes (10 §2 rendition order)."""
    from PIL import EpsImagePlugin

    from snipebot.faces import YuNetDetector

    model = Path(snipebot.__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
    eps = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 40 30\n%%EndComments\n"
           b"% payload-77 image bytes\nshowpage\n%%EOF\n")
    on_disk: list[bytes] = []

    def fake_gs(cmd, **_kw):
        infile = cmd[cmd.index("-f") + 1]
        on_disk.append(Path(infile).read_bytes())
        raise subprocess.CalledProcessError(1, cmd)

    # Simulate a runner with Ghostscript installed; the interpreter itself never runs.
    monkeypatch.setattr(EpsImagePlugin, "has_ghostscript", lambda: True)
    monkeypatch.setattr(EpsImagePlugin, "gs_binary", "gs")
    monkeypatch.setattr(EpsImagePlugin.subprocess, "check_call", fake_gs)

    det = YuNetDetector(str(model), "0.9")
    try:
        det.count_faces(eps)
    except Exception:  # noqa: BLE001 - an undecodable image is fine; the disk write is not
        pass
    assert not any(b"payload-77" in blob for blob in on_disk), (
        "image bytes were written to a temp file for an external decoder"
    )
