"""Transport-level tests for `SlackWebClient` (10-slack-io.md sections 2-3).

Stubs `slack_sdk.WebClient` at the method level: a fake client object exposing
the same per-operation names `slack_sdk.WebClient` has (`conversations_history`,
`auth_test`, `reactions_add`, ...), injected through the constructor, so nothing
here touches the network. `FakeSlack` (tests/fake_slack.py) *replaces* `slack_io`
entirely and is a different layer (50-test-matrix section 2.3); this file never
imports it.
"""

from __future__ import annotations

import email.utils
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import snipebot.slack_io as slack_io
from snipebot.slack_io import (
    HISTORY_PAGE_SIZE,
    MAX_PAGINATION_ITERATIONS,
    MAX_RATE_LIMIT_RETRIES,
    REACTIONS_ADD_PER_MINUTE,
    REACTIONS_REMOVE_PER_MINUTE,
    AlreadyReacted,
    AuthError,
    ChannelNotFound,
    FileTooLarge,
    MessageNotFound,
    MissingScope,
    NoReaction,
    NotInChannel,
    RateLimited,
    SlackAPIError,
    SlackHTTPError,
    SlackPaginationError,
    SlackTransportError,
    SlackWebClient,
)


# --------------------------------------------------------------------------- #
# A stub matching slack_sdk.WebClient's per-method surface: each stubbed method
# name has its own queue of responses/exceptions to return in call order.
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, data, *, status_code: int = 200, headers=None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}


class _ApiError(Exception):
    """Mirrors slack_sdk.errors.SlackApiError's shape: a `.response`."""

    def __init__(self, response: _Resp) -> None:
        self.response = response


class _StubClient:
    def __init__(self, **queues) -> None:
        self._queues = {name: list(q) for name, q in queues.items()}
        self.calls: list[tuple[str, dict]] = []

    def _next(self, name, kwargs):
        self.calls.append((name, dict(kwargs)))
        queue = self._queues.get(name)
        if not queue:
            raise AssertionError(f"no more stubbed responses for {name!r}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def __getattr__(self, name):
        def method(**kwargs):
            return self._next(name, kwargs)

        return method


def _ok(data: dict) -> _Resp:
    body = {"ok": True}
    body.update(data)
    return _Resp(body)


def _err(code: str) -> _Resp:
    return _Resp({"ok": False, "error": code})


def _sleep_recorder():
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)

    return sleep, calls


def _client_for(sleep=None, clock=None, **queues) -> SlackWebClient:
    kwargs = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if clock is not None:
        kwargs["clock"] = clock
    return SlackWebClient(_StubClient(**queues), bot_token="xoxb-test", **kwargs)


# --------------------------------------------------------------------------- #
# L3-FA-rate-limit-io
# --------------------------------------------------------------------------- #

def test_rate_limit_honoured_then_retries_same_request():
    sleep, sleeps = _sleep_recorder()
    client = _client_for(
        sleep=sleep,
        conversations_info=[
            _Resp({"ok": False}, status_code=429, headers={"Retry-After": "7"}),
            _ok({"channel": {"id": "C1"}}),
        ],
    )
    result = client.channel_info("C1")
    assert result == {"id": "C1"}
    assert sleeps == [7]
    stub = client._client
    assert len(stub.calls) == 2
    assert stub.calls[0] == stub.calls[1]  # the identical request was retried


def test_rate_limit_exhausted_raises_rate_limited():
    sleep, sleeps = _sleep_recorder()
    responses = [
        _Resp({"ok": False}, status_code=429, headers={"Retry-After": "1"})
        for _ in range(MAX_RATE_LIMIT_RETRIES + 1)
    ]
    client = _client_for(sleep=sleep, conversations_info=responses)
    with pytest.raises(RateLimited) as exc_info:
        client.channel_info("C1")
    assert exc_info.value.retry_after_seconds == 1
    assert exc_info.value.method == "channel_info"
    assert len(sleeps) == MAX_RATE_LIMIT_RETRIES


def test_reaction_budgets_never_share_tokens():
    now = [1_000.0]

    def clock():
        return now[0]

    sleep, sleeps = _sleep_recorder()
    add_queue = [_ok({}) for _ in range(REACTIONS_ADD_PER_MINUTE + 1)]
    remove_queue = [_ok({})]
    client = _client_for(
        sleep=sleep, clock=clock, reactions_add=add_queue, reactions_remove=remove_queue,
    )
    for _ in range(REACTIONS_ADD_PER_MINUTE):
        client.reactions_add("C1", "1.000001", "thumbsup")
    assert sleeps == []  # exactly at budget: no pacing sleep yet

    # A remove call right after must NOT be blocked by the (now-exhausted) add budget.
    client.reactions_remove("C1", "1.000001", "thumbsup")
    assert sleeps == []

    # The next add call is over budget and must pace.
    client.reactions_add("C1", "1.000001", "thumbsup")
    assert sleeps == [60.0]


# --------------------------------------------------------------------------- #
# L3-FA-fail-mid-page
# --------------------------------------------------------------------------- #

def test_fail_mid_page_raises_nothing_partial():
    client = _client_for(
        conversations_history=[
            _ok({"messages": [{"ts": "1.000001"}],
                 "response_metadata": {"next_cursor": "abc"}}),
            _err("internal_error"),
        ],
    )
    with pytest.raises(SlackAPIError):
        client.history("C1", "0.000000")


# --------------------------------------------------------------------------- #
# L3-FA-repeat-cursor
# --------------------------------------------------------------------------- #

def test_repeat_cursor_raises_pagination_error():
    client = _client_for(
        conversations_history=[
            _ok({"messages": [{"ts": "2.000001"}],
                 "response_metadata": {"next_cursor": "dup"}}),
            _ok({"messages": [{"ts": "1.000001"}],
                 "response_metadata": {"next_cursor": "dup"}}),
        ],
    )
    with pytest.raises(SlackPaginationError):
        client.history("C1", "0.000000")


def test_pagination_loop_guard(monkeypatch):
    monkeypatch.setattr(slack_io, "MAX_PAGINATION_ITERATIONS", 2)
    pages = [
        _ok({"messages": [], "response_metadata": {"next_cursor": f"c{i}"}})
        for i in range(5)
    ]
    client = _client_for(conversations_history=pages)
    with pytest.raises(SlackPaginationError):
        client.history("C1", "0.000000")


# --------------------------------------------------------------------------- #
# L3-FA-empty-page-more
# --------------------------------------------------------------------------- #

def test_empty_page_with_more_keeps_paging():
    client = _client_for(
        conversations_history=[
            _ok({"messages": [], "response_metadata": {"next_cursor": "next"}}),
            _ok({"messages": [{"ts": "1.000001"}], "response_metadata": {}}),
        ],
    )
    result = client.history("C1", "0.000000")
    assert result == [{"ts": "1.000001"}]


def test_history_pages_to_exhaustion_newest_first():
    client = _client_for(
        conversations_history=[
            _ok({"messages": [{"ts": "3.000001"}, {"ts": "2.000001"}],
                 "response_metadata": {"next_cursor": "p2"}}),
            _ok({"messages": [{"ts": "1.000001"}], "response_metadata": {}}),
        ],
    )
    result = client.history("C1", "0.000000", "9.000000")
    assert result == [{"ts": "3.000001"}, {"ts": "2.000001"}, {"ts": "1.000001"}]
    stub = client._client
    first_call = stub.calls[0][1]
    assert first_call["oldest"] == "0.000000"
    assert first_call["latest"] == "9.000000"
    assert first_call["inclusive"] is True
    assert first_call["include_all_metadata"] is True
    assert first_call["limit"] == HISTORY_PAGE_SIZE
    assert "cursor" not in first_call
    assert stub.calls[1][1]["cursor"] == "p2"


def test_users_list_and_conversations_members_page_with_own_cursor():
    client = _client_for(
        users_list=[
            _ok({"members": [{"id": "U1"}], "response_metadata": {"next_cursor": "n"}}),
            _ok({"members": [{"id": "U2"}], "response_metadata": {}}),
        ],
        conversations_members=[
            _ok({"members": ["U1"], "response_metadata": {"next_cursor": "n"}}),
            _ok({"members": ["U2"], "response_metadata": {}}),
        ],
    )
    assert client.users_list() == [{"id": "U1"}, {"id": "U2"}]
    assert client.conversations_members("C1") == ["U1", "U2"]


# --------------------------------------------------------------------------- #
# Error-code -> exception mapping (section 3 table)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "code,expected",
    [
        ("already_reacted", AlreadyReacted),
        ("no_reaction", NoReaction),
        ("message_not_found", MessageNotFound),
        ("not_in_channel", NotInChannel),
        ("channel_not_found", ChannelNotFound),
        ("missing_scope", MissingScope),
        ("not_allowed_token_type", MissingScope),
        ("not_authed", AuthError),
        ("invalid_auth", AuthError),
        ("token_revoked", AuthError),
        ("account_inactive", AuthError),
        ("some_other_error", SlackAPIError),
    ],
)
def test_error_code_mapping(code, expected):
    client = _client_for(conversations_info=[_err(code)])
    with pytest.raises(expected) as exc_info:
        client.channel_info("C1")
    assert exc_info.value.error == code


def test_already_reacted_from_reactions_add():
    client = _client_for(reactions_add=[_err("already_reacted")])
    with pytest.raises(AlreadyReacted):
        client.reactions_add("C1", "1.000001", "thumbsup")


def test_no_reaction_from_reactions_remove():
    client = _client_for(reactions_remove=[_err("no_reaction")])
    with pytest.raises(NoReaction):
        client.reactions_remove("C1", "1.000001", "thumbsup")


def test_message_not_found_from_reactions_get():
    client = _client_for(reactions_get=[_err("message_not_found")])
    with pytest.raises(MessageNotFound):
        client.reactions_get("C1", "1.000001")


def test_non_200_raises_slack_http_error():
    client = _client_for(conversations_info=[_Resp({}, status_code=500)])
    with pytest.raises(SlackHTTPError) as exc_info:
        client.channel_info("C1")
    assert exc_info.value.status == 500


def test_transport_failure_wraps_bare_exception():
    client = _client_for(conversations_info=[ConnectionResetError("reset")])
    with pytest.raises(SlackTransportError):
        client.channel_info("C1")


def test_api_error_shaped_exception_is_read_not_wrapped_as_transport():
    client = _client_for(conversations_info=[_ApiError(_err("channel_not_found"))])
    with pytest.raises(ChannelNotFound):
        client.channel_info("C1")


# --------------------------------------------------------------------------- #
# post_message / update_message / auth_identity / auth_scopes
# --------------------------------------------------------------------------- #

def test_post_message_returns_ts():
    client = _client_for(chat_postMessage=[_ok({"ts": "5.000001"})])
    ts = client.post_message("C1", text="hi", blocks=[], metadata={})
    assert ts == "5.000001"


def test_update_message_returns_ts():
    client = _client_for(chat_update=[_ok({"ts": "5.000001"})])
    ts = client.update_message("C1", "5.000001", text="hi", blocks=[], metadata={})
    assert ts == "5.000001"


def test_auth_identity_normalises_four_fields():
    client = _client_for(
        auth_test=[_ok({"user_id": "U0BOT", "bot_id": "B0BOT", "team_id": "T0",
                        "url": "https://x.slack.com/"})],
    )
    identity = client.auth_identity()
    assert identity.user_id == "U0BOT"
    assert identity.bot_id == "B0BOT"
    assert identity.team_id == "T0"
    assert identity.url == "https://x.slack.com/"


def test_auth_scopes_reads_header():
    resp = _ok({"user_id": "U0"})
    resp.headers = {"x-oauth-scopes": "channels:read, chat:write ,users:read"}
    client = _client_for(auth_test=[resp])
    assert client.auth_scopes() == {"channels:read", "chat:write", "users:read"}


def test_auth_scopes_transport_failure():
    client = _client_for(auth_test=[ConnectionResetError("reset")])
    with pytest.raises(SlackTransportError):
        client.auth_scopes()


# --------------------------------------------------------------------------- #
# fetch_file_bytes
# --------------------------------------------------------------------------- #

class _FakeHTTPResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def test_fetch_file_bytes_streams_with_bearer_header(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeHTTPResponse([b"abc", b"def"])

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = _client_for()
    data = client.fetch_file_bytes("https://files.slack.com/x")
    assert data == b"abcdef"
    assert captured["request"].get_header("Authorization") == "Bearer xoxb-test"
    assert captured["timeout"] == 10


def test_fetch_file_bytes_oversize_raises_file_too_large(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse([b"x" * 100])

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = SlackWebClient(_StubClient(), bot_token="xoxb-test", max_image_bytes=10)
    with pytest.raises(FileTooLarge) as exc_info:
        client.fetch_file_bytes("https://files.slack.com/x")
    assert exc_info.value.limit_bytes == 10


def test_fetch_file_bytes_timeout_raises_transport_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = _client_for()
    with pytest.raises(SlackTransportError):
        client.fetch_file_bytes("https://files.slack.com/x")


def test_fetch_file_bytes_404_raises_slack_http_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError("https://files.slack.com/x", 404, "Not Found", None, None)

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = _client_for()
    with pytest.raises(SlackHTTPError) as exc_info:
        client.fetch_file_bytes("https://files.slack.com/x")
    assert exc_info.value.status == 404


def test_fetch_file_bytes_429_raises_rate_limited(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://files.slack.com/x", 429, "Too Many Requests",
            {"Retry-After": "3"}, None,
        )

    monkeypatch.setattr(slack_io.urllib.request, "urlopen", fake_urlopen)
    client = _client_for()
    with pytest.raises(RateLimited) as exc_info:
        client.fetch_file_bytes("https://files.slack.com/x")
    assert exc_info.value.retry_after_seconds == 3


# --------------------------------------------------------------------------- #
# make_client
# --------------------------------------------------------------------------- #

def test_make_client_builds_a_real_slack_web_client():
    from snipebot.slack_io import make_client

    client = make_client("xoxb-fake-token-for-construction-only")
    assert isinstance(client, SlackWebClient)
    assert client._bot_token == "xoxb-fake-token-for-construction-only"


def test_make_client_threads_fetch_limits_into_the_client():
    from snipebot.slack_io import make_client

    client = make_client(
        "xoxb-fake-token-for-construction-only",
        fetch_timeout_seconds=42,
        max_image_bytes=123456,
    )
    assert client._fetch_timeout_seconds == 42
    assert client._max_image_bytes == 123456


# --------------------------------------------------------------------------- #
# E1: Retry-After parsing -- a missing or non-integer value falls back to 1 s; an
# HTTP-date form is parsed into seconds from now (floor 1); the header never raises.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("headers", [{}, {"Retry-After": "soon"}, {"Retry-After": ""}])
def test_retry_after_missing_or_non_integer_falls_back_to_one(headers):
    sleep, sleeps = _sleep_recorder()
    client = _client_for(
        sleep=sleep,
        conversations_info=[
            _Resp({"ok": False}, status_code=429, headers=headers),
            _ok({"channel": {"id": "C1"}}),
        ],
    )
    assert client.channel_info("C1") == {"id": "C1"}
    assert sleeps == [1]


def test_retry_after_http_date_is_parsed_to_seconds_from_now():
    when = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
    sleep, sleeps = _sleep_recorder()
    client = _client_for(
        sleep=sleep,
        conversations_info=[
            _Resp({"ok": False}, status_code=429, headers={"Retry-After": when}),
            _ok({"channel": {"id": "C1"}}),
        ],
    )
    assert client.channel_info("C1") == {"id": "C1"}
    assert len(sleeps) == 1
    assert 100 <= sleeps[0] <= 121  # ~120 s from now, minus test runtime


def test_retry_after_past_http_date_floors_to_one():
    when = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(seconds=120))
    sleep, sleeps = _sleep_recorder()
    client = _client_for(
        sleep=sleep,
        conversations_info=[
            _Resp({"ok": False}, status_code=429, headers={"Retry-After": when}),
            _ok({"channel": {"id": "C1"}}),
        ],
    )
    assert client.channel_info("C1") == {"id": "C1"}
    assert sleeps == [1]


# --------------------------------------------------------------------------- #
# E9: an ok:false body with error "ratelimited"/"rate_limited" on HTTP 200 maps to
# RateLimited (Retry-After absent -> 1 s) and takes the same reactive retry path.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code", ["ratelimited", "rate_limited"])
def test_ok_false_ratelimited_on_200_honours_retry_then_retries(code):
    sleep, sleeps = _sleep_recorder()
    client = _client_for(
        sleep=sleep,
        conversations_info=[
            _Resp({"ok": False, "error": code}),  # HTTP 200, no Retry-After header
            _ok({"channel": {"id": "C1"}}),
        ],
    )
    assert client.channel_info("C1") == {"id": "C1"}
    assert sleeps == [1]  # Retry-After absent -> 1 s
    assert client._client.calls[0] == client._client.calls[1]  # same request retried


def test_ok_false_ratelimited_on_200_exhausts_to_rate_limited():
    sleep, sleeps = _sleep_recorder()
    responses = [
        _Resp({"ok": False, "error": "ratelimited"})
        for _ in range(MAX_RATE_LIMIT_RETRIES + 1)
    ]
    client = _client_for(sleep=sleep, conversations_info=responses)
    with pytest.raises(RateLimited) as exc_info:
        client.channel_info("C1")
    assert exc_info.value.retry_after_seconds == 1
    assert exc_info.value.method == "channel_info"
    assert len(sleeps) == MAX_RATE_LIMIT_RETRIES


# --------------------------------------------------------------------------- #
# Malformed response: an ok:true reply missing a documented result field raises
# SlackAPIError("malformed_response"); nothing but a SlackError escapes.
# --------------------------------------------------------------------------- #

def test_ok_true_missing_result_field_raises_malformed_response():
    client = _client_for(reactions_get=[_ok({})])  # ok:true, but no "message"
    with pytest.raises(SlackAPIError) as exc_info:
        client.reactions_get("C1", "1.000001")
    assert exc_info.value.error == "malformed_response"


def test_ok_true_post_message_missing_ts_raises_malformed_response():
    client = _client_for(chat_postMessage=[_ok({})])  # ok:true, but no "ts"
    with pytest.raises(SlackAPIError) as exc_info:
        client.post_message("C1", text="hi", blocks=[], metadata={})
    assert exc_info.value.error == "malformed_response"


# --------------------------------------------------------------------------- #
# GitHub Actions workflows (40-config-cli.md section 7.1-7.2): the two YAML
# files parse, and their command mapping / concurrency groups match the spec.
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_workflow(name: str) -> dict:
    path = _REPO_ROOT / ".github" / "workflows" / name
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_sync_workflow_parses_and_matches_spec():
    doc = _load_workflow("sync.yml")
    assert doc["name"] == "sync"
    # PyYAML (YAML 1.1) reads the bare `on:` key as the boolean True; GitHub's
    # own parser special-cases it, but plain yaml.safe_load does not.
    on = doc[True]
    assert on["schedule"] == [{"cron": "7,37 * * * *"}]
    assert on["workflow_dispatch"] == {}
    assert doc["concurrency"] == {"group": "snipebot", "cancel-in-progress": False}
    assert doc["permissions"] == {"contents": "write"}

    steps = doc["jobs"]["sync"]["steps"]
    setup_python = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup_python["uses"] == "actions/setup-python@v6"
    assert setup_python["with"]["python-version"] == "3.11"
    data_checkout = next(
        s for s in steps if s.get("name") == "Checkout data branch"
    )
    assert data_checkout["with"] == {"ref": "data", "path": "_data", "fetch-depth": 0}
    sync_step = next(s for s in steps if s.get("name") == "Sync")
    assert sync_step["run"] == "python -m snipebot sync --data-dir _data/data"
    assert sync_step["env"] == {"SLACK_BOT_TOKEN": "${{ secrets.SLACK_BOT_TOKEN }}"}


def test_admin_workflow_parses_and_matches_spec():
    doc = _load_workflow("admin.yml")
    assert doc["name"] == "admin"
    on = doc[True]
    inputs = on["workflow_dispatch"]["inputs"]
    options = inputs["command"]["options"]
    assert options == [
        "veto", "unveto", "selfie", "rejoin", "accept-deletes", "rules-bump",
        "backfill", "restore", "reevaluate", "purge", "recaps-on", "recaps-off",
    ]
    # No bare "no" key anywhere; the selfie flag is `not_selfie`, a real boolean
    # input mapped to the CLI's --no flag.
    assert "no" not in inputs
    assert inputs["not_selfie"] == {
        "description": "selfie: mark NOT a selfie",
        "required": False,
        "type": "boolean",
        "default": False,
    }
    # Own concurrency group, separate from `snipebot` (40 section 7.2 / 7.1).
    assert doc["concurrency"] == {"group": "snipebot-admin", "cancel-in-progress": False}
    assert doc["permissions"] == {"contents": "write"}

    steps = doc["jobs"]["admin"]["steps"]
    setup_python = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup_python["with"] == {"python-version": "3.11", "cache": "pip"}  # E-W4-33
    run_step = next(s for s in steps if s.get("name") == "Run admin command")
    script = run_step["run"]

    # Every input reaches the shell only through env: indirection, never
    # interpolated directly into the run: script text.
    env = run_step["env"]
    assert env["SLACK_BOT_TOKEN"] == "${{ secrets.SLACK_BOT_TOKEN }}"
    assert env["COMMAND"] == "${{ inputs.command }}"
    assert env["TS"] == "${{ inputs.ts }}"
    assert env["USER_ID"] == "${{ inputs.user }}"  # E-W4-20e: never $USER
    assert "USER" not in env
    assert env["NOT_SELFIE"] == "${{ inputs.not_selfie }}"
    assert env["COUNT"] == "${{ inputs.count }}"
    assert env["FROM"] == "${{ inputs.from }}"
    assert env["EFFECTIVE_FROM"] == "${{ inputs.effective_from }}"
    assert env["CONFIRM_PURGE"] == "${{ inputs.confirm_purge }}"
    assert "${{ inputs." not in script

    # Every env var the script reads is quoted, not bare-interpolated.
    for var in ("$TS", "$USER_ID", "$COUNT", "$FROM", "$EFFECTIVE_FROM", "$CONFIRM_PURGE"):
        assert f'"{var}"' in script, f"{var} not quoted in script"

    # Every admin command choice maps to exactly one `snipebot` invocation in
    # the `case` block; the mapping is content, not just presence.
    expected_commands = {
        "veto": "python -m snipebot $D veto",
        "unveto": "python -m snipebot $D unveto",
        "selfie": "python -m snipebot $D selfie",
        "rejoin": "python -m snipebot $D rejoin",
        "accept-deletes": "python -m snipebot $D accept-deletes",
        "rules-bump": "python -m snipebot $D rules bump",
        "backfill": "python -m snipebot $D backfill",
        "restore": "python -m snipebot $D restore",
        "reevaluate": "python -m snipebot $D sync --reevaluate",
        "purge": "python -m snipebot $D purge",
    }
    for command, expected_prefix in expected_commands.items():
        assert f"{command})" in script, f"missing case branch for {command!r}"
        assert expected_prefix in script, f"missing invocation for {command!r}"
    assert 'test "$CONFIRM_PURGE" = "ERASE"' in script
    assert "--rewrite-history --yes" in script
    assert "--no-react" in script  # backfill's admin invocation (plan section 8)

    # --by is only passed to veto/selfie when the user input is non-empty, so
    # the CLI's admins[0] default stays reachable when it's omitted. E-W4-20e:
    # --by and --no travel as quoted array elements (40 section 7.2), so the
    # free-text user input is never word-split into extra CLI arguments.
    assert 'BY=(); [ -n "$USER_ID" ] && BY=(--by "$USER_ID")' in script
    assert 'N=(); [ "$NOT_SELFIE" = "true" ] && N=(--no)' in script
    assert 'veto   --ts "$TS" "${BY[@]}"' in script
    assert 'selfie --ts "$TS" "${BY[@]}" "${N[@]}"' in script
    assert "$BY " not in script and "$N " not in script and not script.rstrip().endswith("$N")

    # The config commit step (rules-bump, recaps-on/off, E-W4-39) lands config.yaml on the
    # code branch only, never through the data store, and echoes the resulting commit SHA.
    commit_step = next(s for s in steps if s.get("name") == "Commit config change")
    assert commit_step["if"] == (
        "${{ inputs.command == 'rules-bump' || inputs.command == 'recaps-on'"
        " || inputs.command == 'recaps-off' }}")
    commit_script = commit_step["run"]
    assert "git add config.yaml" in commit_script
    assert "rules bump effective $EFFECTIVE_FROM" in commit_script
    assert "git push" in commit_script
    assert "git rev-parse HEAD" in commit_script
    assert commit_step["env"]["EFFECTIVE_FROM"] == "${{ inputs.effective_from }}"
