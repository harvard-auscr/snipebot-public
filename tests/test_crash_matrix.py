"""L3 crash matrix (50 section 5): run the real `sync` in a subprocess against the
file-backed fake, kill it at each named boundary (20 section 2.2), re-run to completion,
and assert convergence (C1-C3, 50 section 5.2).

The fake's Slack world lives in a JSON file outside the killed subprocess (10 section 7),
so an added reaction or a posted digest survives the kill. The subprocess is
``python -m tests._fake_entry`` pointed at that world via the environment; `_boundary`
reads ``SNIPEBOT_CRASH_AT`` / ``SNIPEBOT_CRASH_KEY`` and calls ``os._exit(137)`` at the
matching boundary.

Persistence is `files` (the seam `_fake_entry` drives): `ledger.save_*` writes atomically
and `FilesStore` has nothing to commit, so the four git commit/push boundaries
(`before_commit` .. `after_push`) never fire here -- they belong to the git-persistence
suite. Every boundary the files backend *does* reach is exercised, including the per-message
`faces:*` / `reaction:*` (keyed on each message ts, so a crash between two detections and
between two reactions is covered) and the per-report `digest:*`.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.ledger import load_state
from snipebot.sync import run_sync
from snipebot.ts import US_PER_SECOND, parse_ts

from tests.fake_slack import FakeSlack, FakeUser

# --- world constants ---------------------------------------------------------

CHANNEL = "C0MAIN01"
BOT = "U0BOTXX1"
A, B, C, D = "U0AAAAA1", "U0BBBBB1", "U0CCCCC1", "U0DDDDD1"

IMG_A = b"snap-alpha"
IMG_B = b"snap-bravo"
IMG_A2 = b"snap-alpha-2"   # M1's second image, so its per-image faces:* boundary fires twice


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ts(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, micro: int = 1) -> str:
    return f"{calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))}.{micro:06d}"


# Two independent counted snipes on 2026-09-18: M1 carries TWO images (so its per-image
# `faces:*` boundary fires twice under one message key -- the §5.1 2nd-ordinal per-item
# boundary), M2 one; each message earns a `white_check_mark`. Synced at 21:30 when the daily
# digest is due.
M1 = _ts(2026, 9, 18, 12)
M2 = _ts(2026, 9, 18, 13)
NOW = _ts(2026, 9, 18, 21, 30, micro=0)
DKEY = f"{CHANNEL}:daily:2026-09-18"

FACES_MAP = {_sha(IMG_A): 1, _sha(IMG_A2): 1, _sha(IMG_B): 1}

_CONFIG_YAML = """\
enabled: true
persistence: files
slack:
  channel: C0MAIN01
timezone: UTC
sync:
  interval_minutes: 10
  scan_days: 14
  history_horizon_days: 90
  max_deletes_per_run: 5
  large_movement_rows: 25
semesters:
  - name: fall-2026
    start: 2026-09-01
    end: 2026-12-18
rules:
  cooldown:
    minutes: 15
    scope: pair
    rejected_attempts_reset: false
  multi_tag: per_target
  max_targets_per_message: null
  edit_grace_minutes: 10
  max_snipes_per_target_per_day: null
  allow_self: false
  allow_bots: false
  count_thread_replies: false
  count_image_links: false
  allow_video: false
  selfie_bonus: true
players:
  count_intra_group: true
  groups:
    fam:
      - U0AAAAA1
      - U0BBBBB1
      - U0CCCCC1
      - U0DDDDD1
consent:
  veto:
    emoji: no_entry_sign
    by:
      - admins
  optout_messages: []
  opted_out: []
admins:
  - U0ADMIN1
feedback:
  reactions:
    counted: white_check_mark
    cooldown: hourglass_flowing_sand
    untagged: null
    not_counted: x
    selfie: null
  review:
    min_targets: null
    emoji: null
reports:
  - name: daily
    every: 1d
    at: "21:00"
    sections: [day]
    top_n: 5
faces:
  model_path: models/face.onnx
  fetch_timeout_seconds: 5
  max_image_bytes: 1000000
  max_attempts: 3
  score_threshold: "0.9"
"""


def _image(file_id: str, data: bytes) -> dict:
    return {
        "id": file_id, "mimetype": "image/png", "name": "snap.png", "size": len(data),
        "original_w": 100, "original_h": 100,
        "thumb_1024": f"https://files.example/{file_id}",
        "url_private_download": f"https://files.example/{file_id}/dl",
        "_bytes": data,
    }


def _author_world() -> dict:
    """A two-snipe world, materialised as a world dict at simulated now = NOW."""
    users = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in (A, B, C, D):
        users[uid] = FakeUser(id=uid, display_name=uid)
    fake = FakeSlack(now=NOW, bot_user_id=BOT, users=users,
                     channels=(CHANNEL,), bot_member_of=(CHANNEL,))
    fake.post(at=M1, user=A, channel=CHANNEL, text=f"<@{B}>",
              files=[_image("F01", IMG_A), _image("F03", IMG_A2)])
    fake.post(at=M2, user=C, channel=CHANNEL, text=f"<@{D}>", files=[_image("F02", IMG_B)])
    return fake.to_world_dict()


# --- materialisation helpers (read back the fake's world) --------------------

def _bot_reactions(world_path: Path, message_ts: list[str]) -> dict[str, frozenset[str]]:
    """The bot's reactions actually present on each scan-window message (C2)."""
    inner = FakeSlack.from_world_dict(json.loads(world_path.read_text(encoding="utf-8")))
    out: dict[str, frozenset[str]] = {}
    for ts in message_ts:
        rec = inner.reactions_get(CHANNEL, ts)
        out[ts] = frozenset(
            r["name"] for r in rec.get("reactions", []) if BOT in r.get("users", [])
        )
    return out


def _digests(world_path: Path) -> dict[tuple[str, str], tuple[str, int]]:
    """Every snipe_digest *post* in the world, keyed on (channel, period_key) -> its
    converged (numbers_hash, revision). A duplicate key would collapse here, so the
    caller asserts the count too (C3)."""
    inner = FakeSlack.from_world_dict(json.loads(world_path.read_text(encoding="utf-8")))
    posts = [
        e for e in inner._events
        if e["kind"] == "post"
        and (e["data"].get("metadata") or {}).get("event_type") == "snipe_digest"
    ]
    out: dict[tuple[str, str], tuple[str, int]] = {}
    for e in posts:
        payload = e["data"]["metadata"]["event_payload"]
        out[(payload["channel"], payload["period_key"])] = (
            payload["numbers_hash"], payload["revision"],
        )
    # count of raw posts, so a second post of the same key is caught by the caller
    out["__count__"] = len(posts)  # type: ignore[index]
    return out


# --- fixtures ----------------------------------------------------------------

@dataclass(frozen=True)
class Kit:
    cfg_path: Path
    world_template: Path


@dataclass(frozen=True)
class Reference:
    ledger: bytes
    verdicts: bytes
    reactions: dict[str, frozenset[str]]
    digests: dict


@pytest.fixture(scope="module")
def kit(tmp_path_factory) -> Kit:
    base = tmp_path_factory.mktemp("crash_base")
    cfg_path = base / "config.yaml"
    cfg_path.write_text(_CONFIG_YAML, encoding="utf-8")
    world = base / "world.json"
    world.write_text(
        json.dumps(_author_world(), ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return Kit(cfg_path=cfg_path, world_template=world)


def _fresh(dst: Path, kit: Kit) -> tuple[Path, Path, Path]:
    """A private world copy + empty data dir under `dst`; returns (world, ledger, state)."""
    world = dst / "world.json"
    shutil.copy(kit.world_template, world)
    data = dst / "data"
    data.mkdir(exist_ok=True)
    return world, data / "ledger.jsonl", data / "state.json"


def _run_inproc(kit: Kit, world: Path, ledger: Path, state: Path):
    from tests.fake_slack import FileBackedFakeSlack

    cfg = load_config(str(kit.cfg_path))
    slack = FileBackedFakeSlack(path=str(world))
    return run_sync(
        slack, cfg, detector=FakeFaceDetector(dict(FACES_MAP)),
        ledger_path=ledger, state_path=state, now_us=parse_ts(NOW),
    )


@pytest.fixture(scope="module")
def reference(kit, tmp_path_factory) -> Reference:
    """An uninterrupted control run: same world, same `now`. C1-C3 are asserted against it."""
    d = tmp_path_factory.mktemp("crash_control")
    world, ledger, state = _fresh(d, kit)
    result = _run_inproc(kit, world, ledger, state)
    assert result.exit_code == 0
    verdicts = ledger.with_name("verdicts.jsonl")
    return Reference(
        ledger=ledger.read_bytes(),
        verdicts=verdicts.read_bytes(),
        reactions=_bot_reactions(world, [M1, M2]),
        digests=_digests(world),
    )


def _run_entry(kit: Kit, world: Path, ledger: Path, state: Path, *,
               crash_at: str, crash_key: str | None, crash_nth: int | None = None) -> int:
    """Launch `python -m tests._fake_entry` against the file-backed world and return its
    exit code. With a crash env armed it dies at the boundary (os._exit(137)); an armed
    `crash_nth` (20 §2.2) gates it on the Nth occurrence of that (boundary, key)."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        SNIPEBOT_FAKE_SLACK=str(world),
        SNIPEBOT_CONFIG=str(kit.cfg_path),
        SNIPEBOT_FAKE_FACES=json.dumps(FACES_MAP),
        SNIPEBOT_LEDGER=str(ledger),
        SNIPEBOT_STATE=str(state),
        SNIPEBOT_NOW_US=str(parse_ts(NOW)),
        SNIPEBOT_CRASH_AT=crash_at,
        PYTHONPATH=str(root),
    )
    if crash_key is not None:
        env["SNIPEBOT_CRASH_KEY"] = crash_key
    if crash_nth is not None:
        env["SNIPEBOT_CRASH_NTH"] = str(crash_nth)
    proc = subprocess.run(
        [sys.executable, "-m", "tests._fake_entry"],
        cwd=str(root), env=env, capture_output=True, timeout=120,
    )
    return proc.returncode


# --- the matrix --------------------------------------------------------------

# Every boundary the files backend reaches, with its per-message / per-report key and, where
# a key hosts more than one occurrence, the 1st/2nd ordinal (SNIPEBOT_CRASH_NTH, 20 §2.2).
# `m1` carries two images, so `faces:*[m1]` fires twice: nth=1 crashes between the two
# per-image detections' start (before the 2nd), nth=2 after the 2nd -- the §5.1 2nd-ordinal
# per-item boundary. `m2` and the digest host a single occurrence each.
_MATRIX: list[tuple[str, str | None, int | None]] = [
    ("start", None, None),
    ("after_load", None, None),
    ("after_fetch", None, None),
    ("after_parse", None, None),
    ("after_merge", None, None),
    ("faces:before", "m1", None),
    ("faces:before", "m1", 2),      # crash before the 2nd per-image detection of m1
    ("faces:before", "m2", None),
    ("faces:after", "m1", None),
    ("faces:after", "m1", 2),       # crash after the 2nd per-image detection of m1
    ("faces:after", "m2", None),
    ("after_faces", None, None),
    ("after_consent", None, None),
    ("after_evaluate", None, None),
    ("reaction:before", "m1", None),
    ("reaction:before", "m2", None),
    ("reaction:after", "m1", None),
    ("reaction:after", "m2", None),
    ("after_reactions", None, None),
    ("before_persist", None, None),
    ("after_persist", None, None),
    ("digest:before", "dkey", None),
    ("digest:after", "dkey", None),
    ("after_digests", None, None),
    ("done", None, None),
]

_KEYS = {None: None, "m1": M1, "m2": M2, "dkey": DKEY}


def _param_id(boundary: str, sel: str | None, nth: int | None) -> str:
    tag = boundary.replace(":", "-")
    if sel is not None:
        tag = f"{tag}[{sel}]"
    if nth is not None:
        tag = f"{tag}#{nth}"
    return tag


@pytest.mark.parametrize(
    "boundary,key_sel,crash_nth", _MATRIX,
    ids=[_param_id(b, s, n) for b, s, n in _MATRIX],
)
def test_crash_at_boundary_converges(kit, reference, tmp_path, boundary, key_sel, crash_nth):
    world, ledger, state = _fresh(tmp_path, kit)
    key = _KEYS[key_sel]

    # 1) crash the real sync at the boundary; it must actually die there.
    rc = _run_entry(kit, world, ledger, state, crash_at=boundary, crash_key=key,
                    crash_nth=crash_nth)
    assert rc == 137, f"boundary {boundary!r} did not fire (exit {rc})"

    # state.json is never observed half-written: it is a single atomic rename, so at the
    # kill point it is either absent or a whole, loadable file.
    if state.exists():
        load_state(state)

    # 2) a clean re-run drives the sync to completion.
    result = _run_inproc(kit, world, ledger, state)
    assert result.exit_code == 0

    # C1: the final ledger.jsonl (and verdicts.jsonl) are byte-identical to the control.
    assert ledger.read_bytes() == reference.ledger
    assert ledger.with_name("verdicts.jsonl").read_bytes() == reference.verdicts

    # C2: the bot's reactions converge to the desired set, at most three per message.
    reactions = _bot_reactions(world, [M1, M2])
    assert reactions == reference.reactions
    assert all(len(names) <= 3 for names in reactions.values())

    # C3: exactly one digest per (channel, period_key), converged metadata, no duplicate.
    digests = _digests(world)
    assert digests == reference.digests
    assert digests["__count__"] == 1


# --- positive control: a non-atomic persist would break C1 -------------------

def test_positive_control_torn_ledger_breaks_c1(kit, reference, tmp_path):
    """The convergence of C1 rests on the atomic ledger write (50 section 5.2). Replace it
    with a torn two-step write -- a ledger.jsonl left half-written by a kill between the
    steps -- and the next run cannot recover it: load_ledger aborts (exit 4) and the file
    stays torn, so C1 (byte-identical to the control) fails. This proves the assertion has
    teeth; the real atomic rename never leaves such a file."""
    world, ledger, state = _fresh(tmp_path, kit)

    torn = reference.ledger[:-5]             # last row truncated mid-JSON
    assert torn != reference.ledger
    ledger.write_bytes(torn)

    result = _run_inproc(kit, world, ledger, state)

    assert result.exit_code == 4             # MalformedLedgerError -> writes nothing
    assert not result.ledger_written
    assert ledger.read_bytes() == torn       # never recovered
    assert ledger.read_bytes() != reference.ledger   # C1 would fail under a non-atomic write
