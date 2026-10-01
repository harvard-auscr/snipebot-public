"""Wave-4 rulings on the Actions workflows and the runtime requirements: E-W4-20e (admin.yml
passes `--by` / `--no` as quoted array elements from `USER_ID`) and E-W4-33 (job timeouts,
the pip cache, exact runtime pins)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.red_team.wave4.test_deploy_r2 import _admin_argv

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"

PINS = {
    "slack_sdk": "3.44.1",
    "PyYAML": "6.0.3",
    "openpyxl": "3.1.5",
    "et_xmlfile": "2.0.0",
    "tzdata": "2026.4",
    "opencv-python-headless": "4.14.0.94",
    "numpy": "2.4.6",
    "pillow": "12.3.0",
    "pillow_heif": "0.22.0",
}


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _job(name: str) -> dict:
    doc = _load(name)
    (job,) = doc["jobs"].values()
    return job


def _admin_step() -> dict:
    return next(s for s in _job("admin.yml")["steps"] if s.get("name") == "Run admin command")


# --- E-W4-20e ------------------------------------------------------------------------------

def test_admin_user_input_binds_to_user_id_env():
    """E-W4-20e: the `user` dispatch input reaches the shell as `USER_ID` (40 section 7.2),
    never as `USER` (the runner's own login variable), and the script never reads `$USER`."""
    step = _admin_step()
    assert step["env"]["USER_ID"] == "${{ inputs.user }}"
    assert "USER" not in step["env"]
    assert not re.search(r"\$USER\b", step["run"])
    assert "BY=(); [ -n \"$USER_ID\" ] && BY=(--by \"$USER_ID\")" in step["run"]
    assert '"${BY[@]}"' in step["run"] and '"${N[@]}"' in step["run"]


def test_admin_veto_without_user_omits_by(tmp_path):
    """E-W4-20e: an empty `user` input passes no `--by`, so the CLI's admins[0] default is
    reachable; a plain ID passes exactly `--by <id>`."""
    bare = _admin_argv({"command": "veto", "ts": "1790000000.000001"}, tmp_path)
    assert bare == ["--data-dir", "_data/data", "veto", "--ts", "1790000000.000001"], bare
    by = _admin_argv({"command": "veto", "ts": "1790000000.000001", "user": "U0AAA009"},
                     tmp_path)
    assert by[-2:] == ["--by", "U0AAA009"], by


def test_admin_selfie_by_and_no_are_single_arguments(tmp_path):
    """E-W4-20e: selfie with a spaced `user` keeps it one argument, and not_selfie maps to
    one `--no`; nothing is word-split into extra flags."""
    argv = _admin_argv(
        {"command": "selfie", "ts": "1790000000.000001",
         "user": "U0AAA009 --no", "not_selfie": "true"},
        tmp_path,
    )
    assert argv == ["--data-dir", "_data/data", "selfie", "--ts", "1790000000.000001",
                    "--by", "U0AAA009 --no", "--no"], argv


def test_admin_rejoin_and_purge_read_user_id(tmp_path):
    """E-W4-20e: rejoin and purge pass the `user` input as one quoted argument from
    `USER_ID`."""
    rejoin = _admin_argv({"command": "rejoin", "user": "U0AAA002"}, tmp_path)
    assert rejoin[-1] == "U0AAA002", rejoin
    purge = _admin_argv({"command": "purge", "user": "U0AAA003", "confirm_purge": "ERASE"},
                        tmp_path)
    assert purge[purge.index("--user") + 1] == "U0AAA003", purge


# --- E-W4-33 -------------------------------------------------------------------------------

def test_both_jobs_time_out_after_15_minutes():
    """E-W4-33: `timeout-minutes: 15` on the sync job and the admin job."""
    for name in ("sync.yml", "admin.yml"):
        assert _job(name).get("timeout-minutes") == 15, name


def test_both_workflows_cache_pip():
    """E-W4-33: `actions/setup-python` runs with `cache: pip` in both workflows, on 3.11."""
    for name in ("sync.yml", "admin.yml"):
        setup = next(s for s in _job(name)["steps"]
                     if str(s.get("uses", "")).startswith("actions/setup-python"))
        assert setup["with"].get("cache") == "pip", name
        assert setup["with"]["python-version"] == "3.11", name


def test_requirements_pin_the_exact_tested_runtime():
    """E-W4-33: requirements.txt pins every runtime package with `==` to the tested version,
    and carries nothing unpinned."""
    lines = [ln.split("#", 1)[0].strip()
             for ln in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    reqs = [ln for ln in lines if ln]
    pinned = {}
    for req in reqs:
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([0-9][0-9A-Za-z.]*)", req)
        assert m, f"not an exact pin: {req!r}"
        pinned[m.group(1)] = m.group(2)
    assert pinned == PINS
