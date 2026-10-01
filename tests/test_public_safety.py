"""Two guards for a copy of this repo that ends up public.

Every workflow job's first step stops the run unless the repo is private: the `data` branch,
the run logs and the `standings` artifact hold Slack IDs and display names, and a public repo
would serve all three to anyone. The check asks the API rather than reading
`github.event.repository`, which a scheduled run's payload may not carry (a job-level `if:` on
it would then skip every scheduled sync).

The data-branch commits are authored by an address no GitHub account can own: a
`users.noreply.github.com` address belongs to whoever holds that username.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from snipebot.persistence import DEFAULT_GIT_AUTHOR

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_stops_first_unless_the_repo_is_private(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name, job in doc["jobs"].items():
        assert "if" not in job, f"{path.name}:{name}: a job-level if can skip scheduled runs"
        first = job["steps"][0]
        assert "private" in first["name"].lower()
        assert first["env"]["GH_TOKEN"] == "${{ github.token }}"
        assert first["run"] == (
            'test "$(gh api "repos/${{ github.repository }}" --jq .private)" = "true"'
        )


def test_there_are_workflows_to_check():
    assert {p.name for p in WORKFLOWS} >= {"sync.yml", "admin.yml", "export.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_commit_identity_a_github_user_could_own(path):
    assert "users.noreply.github.com" not in path.read_text(encoding="utf-8")


def test_default_git_author_is_unownable():
    assert DEFAULT_GIT_AUTHOR.endswith("@example.invalid>")
