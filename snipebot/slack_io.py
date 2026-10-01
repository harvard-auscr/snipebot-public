"""Boundary declarations for the Slack transport: types, the SlackIO protocol and the
error taxonomy, plus the real transport (backed by slack_sdk) below the taxonomy.
"""

from __future__ import annotations

import email.utils
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

# --- 1. Type aliases and constants ---------------------------------------------------

SlackTs = str                    # a Slack timestamp string, e.g. "1758210000.000199"
RawMessage = dict[str, Any]      # a raw Slack message object, unmodified from the API
RawUser = dict[str, Any]         # a raw Slack user object from users.list
RawChannel = dict[str, Any]      # a raw Slack channel object from conversations.info

HISTORY_PAGE_SIZE: int = 200                 # Slack's recommended page size; hard max 999
MAX_PAGINATION_ITERATIONS: int = 10_000      # loop guard for a repeated/duplicated cursor
MAX_RATE_LIMIT_RETRIES: int = 8              # consecutive 429s honoured on one call, then raise
REACTIONS_ADD_PER_MINUTE: int = 50           # Tier 3 pacing target for reactions.add
REACTIONS_REMOVE_PER_MINUTE: int = 20        # Tier 2 pacing target for reactions.remove


# --- 2. The SlackIO protocol -----------------------------------------------------------

@dataclass(frozen=True)
class AuthIdentity:
    user_id: str        # the bot's OWN user id -> this is the bot_user_id passed to parse()
    bot_id: str         # the app's bot_id (the "B…" id that appears on bot messages)
    team_id: str
    url: str            # workspace url (used only by doctor's human-readable output)


class SlackIO(Protocol):
    def auth_identity(self) -> AuthIdentity:
        """Return the bot's own identity (auth.test)."""
        ...

    def history(
        self, channel: str, oldest: SlackTs, latest: SlackTs | None = None
    ) -> list[RawMessage]:
        """Return every message in [oldest, latest] (inclusive), newest-first, fully paged."""
        ...

    def reactions_get(self, channel: str, ts: SlackTs) -> RawMessage:
        """Return one message with complete, untruncated reaction user lists."""
        ...

    def reactions_add(self, channel: str, ts: SlackTs, name: str) -> None:
        """Add a reaction to a message."""
        ...

    def reactions_remove(self, channel: str, ts: SlackTs, name: str) -> None:
        """Remove a reaction from a message."""
        ...

    def post_message(
        self, channel: str, *, text: str, blocks: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> SlackTs:
        """Post a new message and return its ts."""
        ...

    def update_message(
        self, channel: str, ts: SlackTs, *, text: str, blocks: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> SlackTs:
        """Edit an existing message in place and return its (unchanged) ts."""
        ...

    def users_list(self) -> list[RawUser]:
        """Return every workspace user, fully paged."""
        ...

    def channel_info(self, channel: str) -> RawChannel:
        """Return one channel's info."""
        ...

    def conversations_members(self, channel: str) -> list[str]:
        """Return every member user id of a channel, fully paged."""
        ...

    def fetch_file_bytes(self, url: str) -> bytes:
        """Fetch a file's private rendition bytes over HTTP (not a Web API method)."""
        ...


# --- 3. Error taxonomy -----------------------------------------------------------------

class SlackError(Exception):
    """Base for everything SlackIO raises."""


class SlackTransportError(SlackError):
    """No usable HTTP response: connection reset, timeout, or a response the
    client never received (see the lose_post_response fault, §6)."""


class SlackHTTPError(SlackError):
    """A non-200, non-429 HTTP status."""
    status: int


class RateLimited(SlackError):
    """HTTP 429. Raised only after MAX_RATE_LIMIT_RETRIES have been honoured."""
    retry_after_seconds: int
    method: str                 # "history" | "reactions_add" | ... , for the log


class SlackPaginationError(SlackError):
    """A repeated cursor or a page count over the loop guard."""


class SlackAPIError(SlackError):
    """HTTP 200 with ok:false; `error` is Slack's error code string."""
    error: str


class FileTooLarge(SlackError):
    """`fetch_file_bytes` streamed past `faces.max_image_bytes` before the body ended.
    Not a Slack API error (no ok:false) and not an HTTP status: raised at the fetch
    boundary the moment the running byte count exceeds the cap, so an oversized rendition
    is never fully buffered. Tolerated only by the sync faces block (20 §5): that image
    gets no count this run and the run continues."""
    limit_bytes: int            # the faces.max_image_bytes cap that was exceeded


# --- tolerated by specific callers (see the tolerance table) ---
class AlreadyReacted(SlackAPIError):    ...   # error == "already_reacted"
class NoReaction(SlackAPIError):        ...   # error == "no_reaction"
class MessageNotFound(SlackAPIError):   ...   # error == "message_not_found"

# --- fatal; mapped to distinct classes so doctor can print a precise cause ---
class NotInChannel(SlackAPIError):      ...   # error == "not_in_channel"
class ChannelNotFound(SlackAPIError):   ...   # error == "channel_not_found"
class MissingScope(SlackAPIError):      ...   # error == "missing_scope" (or "not_allowed_token_type")
class AuthError(SlackAPIError):         ...   # not_authed | invalid_auth | token_revoked | account_inactive


# --- 4. The real transport (io+manifest workstream) -----------------------------------

_ERROR_CLASSES: dict[str, type] = {
    "already_reacted": AlreadyReacted,
    "no_reaction": NoReaction,
    "message_not_found": MessageNotFound,
    "not_in_channel": NotInChannel,
    "channel_not_found": ChannelNotFound,
    "missing_scope": MissingScope,
    "not_allowed_token_type": MissingScope,
    "not_authed": AuthError,
    "invalid_auth": AuthError,
    "token_revoked": AuthError,
    "account_inactive": AuthError,
}


def _error_for(code: str) -> SlackAPIError:
    cls = _ERROR_CLASSES.get(code, SlackAPIError)
    exc = cls(code)
    exc.error = code
    return exc


# ok:false error codes that mean "rate limited" on an HTTP 200 body (E9): they
# map to RateLimited and take the same reactive retry path as a 429.
_RATE_LIMITED_ERRORS: frozenset[str] = frozenset({"ratelimited", "rate_limited"})


def _parse_retry_after(headers: Mapping[str, Any] | None) -> int:
    """`Retry-After` -> whole seconds to wait (E1). A missing or non-integer value
    falls back to 1; an HTTP-date form is parsed (email.utils.parsedate_to_datetime)
    into seconds from now, floored at 1. The header never raises."""
    h = headers or {}
    raw = h.get("Retry-After")
    if raw is None:
        # Header names are case-insensitive (RFC 9110 section 5.1): slack_sdk hands
        # an ok:false body's headers over as a plain dict in the server's casing.
        try:
            raw = next(
                (v for k, v in h.items() if str(k).lower() == "retry-after"), None
            )
        except (AttributeError, TypeError):
            raw = None
    if raw is None:
        return 1
    text = str(raw).strip()
    try:
        return max(1, int(text))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return 1
    if when is None:
        return 1
    try:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError, OSError):
        return 1
    return max(1, int(seconds))


def _require(data: Mapping[str, Any], key: str) -> Any:
    """Read a documented result field off an ok:true body, or raise the taxonomy's
    SlackAPIError('malformed_response') when it is absent -- so a malformed but
    ok:true payload never leaks a bare KeyError out of a SlackIO method."""
    try:
        return data[key]
    except (KeyError, TypeError):
        exc = SlackAPIError("malformed_response")
        exc.error = "malformed_response"
        raise exc from None


class _RateBucket:
    """A sliding one-minute window over `per_minute` calls, paced on an injected
    clock (10 section 3). `reactions.add` and `reactions.remove` each get one of
    these; they never share tokens."""

    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._times: list[float] = []

    def wait(self, sleep: Callable[[float], None]) -> None:
        now = self._clock()
        self._times = [t for t in self._times if t > now - 60.0]
        if len(self._times) >= self._per_minute:
            delay = self._times[0] + 60.0 - now
            if delay > 0:
                sleep(delay)
            now = self._clock()
            self._times = [t for t in self._times if t > now - 60.0]
        self._times.append(now)


class SlackWebClient:
    """The real `SlackIO`, backed by a `slack_sdk.WebClient` (or, in tests, a stub
    exposing the same per-operation method names -- `conversations_history`,
    `auth_test`, `reactions_add`, and so on -- the shape `slack_sdk.WebClient`
    itself exposes). Every Web API call goes through `_call`, which turns Slack's
    `ok:false` / HTTP 429 / non-200 / dropped-response outcomes into the section 3
    taxonomy; nothing else in the package touches `slack_sdk` directly.

    DECISION (writer, io+manifest): the constructor takes the underlying client as
    its first argument precisely so tests never touch the network (ground rules);
    `make_client(token)` is the only caller that builds a real `slack_sdk.WebClient`.
    """

    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        fetch_timeout_seconds: int = 10,
        max_image_bytes: int = 25_000_000,
        bot_token: str | None = None,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._max_image_bytes = max_image_bytes
        self._bot_token = bot_token
        self._add_bucket = _RateBucket(REACTIONS_ADD_PER_MINUTE, clock)
        self._remove_bucket = _RateBucket(REACTIONS_REMOVE_PER_MINUTE, clock)

    # -- the one call path every Web API operation goes through ---------------

    def _call(
        self, method: str, *, label: str | None = None, **kwargs: Any
    ) -> Mapping[str, Any]:
        # `method` is the Web API operation invoked on the client; `label` is the
        # SlackIO method name recorded on RateLimited for the log (10 §3). They
        # coincide for reactions_* / users_list / conversations_members and diverge
        # for e.g. history <- conversations_history, post_message <- chat_postMessage.
        name = label or method
        fn = getattr(self._client, method)
        attempts = 0
        while True:
            try:
                resp = fn(**kwargs)
            except Exception as exc:  # slack_sdk raises SlackApiError(response=...);
                resp = getattr(exc, "response", None)  # a stub may raise the same shape.
                if resp is None:
                    raise SlackTransportError(f"{name}: {exc}") from exc
            status = int(getattr(resp, "status_code", 200) or 200)
            data = getattr(resp, "data", resp)
            # Classify by HTTP status BEFORE requiring a JSON body: a 429/non-200 with
            # an empty or non-JSON body (a CDN/proxy page) still carries a real status
            # and must map to RateLimited / SlackHTTPError, not SlackTransportError.
            if status == 429:
                retry_after = _parse_retry_after(getattr(resp, "headers", None))
                attempts += 1
                if attempts > MAX_RATE_LIMIT_RETRIES:
                    exc2 = RateLimited(f"{name}: rate limited")
                    exc2.retry_after_seconds = retry_after
                    exc2.method = name
                    raise exc2
                self._sleep(retry_after)
                continue
            if status != 200:
                exc3 = SlackHTTPError(f"{name}: HTTP {status}")
                exc3.status = status
                raise exc3
            if not isinstance(data, Mapping):
                raise SlackTransportError(f"{name}: no usable response body")
            if not data.get("ok", False):
                error = str(data.get("error", ""))
                # E9: an ok:false "ratelimited"/"rate_limited" body on HTTP 200 is a
                # rate limit; honour Retry-After (absent -> 1 s) and take the same
                # reactive retry path as a 429.
                if error in _RATE_LIMITED_ERRORS:
                    retry_after = _parse_retry_after(getattr(resp, "headers", None))
                    attempts += 1
                    if attempts > MAX_RATE_LIMIT_RETRIES:
                        exc2 = RateLimited(f"{name}: rate limited")
                        exc2.retry_after_seconds = retry_after
                        exc2.method = name
                        raise exc2
                    self._sleep(retry_after)
                    continue
                raise _error_for(error)
            return data

    def _paged(
        self, method: str, *, item_key: str, label: str | None = None,
        required: bool = False, **kwargs: Any
    ) -> list[Any]:
        # `required`: the item field is a documented result field whose absence on
        # an ok:true page raises SlackAPIError("malformed_response") (10 section 2).
        items: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > MAX_PAGINATION_ITERATIONS:
                raise SlackPaginationError(f"{method}: exceeded the pagination loop guard")
            call_kwargs = dict(kwargs)
            if cursor:
                call_kwargs["cursor"] = cursor
            data = self._call(method, label=label, **call_kwargs)
            page = _require(data, item_key) if required else data.get(item_key)
            items.extend(page or [])
            next_cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not next_cursor:
                return items
            if next_cursor in seen:
                raise SlackPaginationError(f"{method}: repeated cursor")
            seen.add(next_cursor)
            cursor = next_cursor

    # -- SlackIO ----------------------------------------------------------------

    def auth_identity(self) -> AuthIdentity:
        data = self._call("auth_test", label="auth_identity")
        return AuthIdentity(
            user_id=str(data.get("user_id", "")),
            bot_id=str(data.get("bot_id", "")),
            team_id=str(data.get("team_id", "")),
            url=str(data.get("url", "")),
        )

    def auth_scopes(self) -> frozenset[str]:
        """Scopes granted to the token, read from the `x-oauth-scopes` header of
        `auth.test` (40 section 5.2 DOC-SCOPES). Deliberately not part of the
        `SlackIO` protocol -- header data has no place in the normalised
        `AuthIdentity` -- so this is called duck-typed, only by `doctor`."""
        fn = getattr(self._client, "auth_test")
        attempts = 0
        while True:
            try:
                resp = fn()
            except Exception as exc:
                resp = getattr(exc, "response", None)
                if resp is None:
                    raise SlackTransportError(f"auth_test: {exc}") from exc
            status = int(getattr(resp, "status_code", 200) or 200)
            # Classify by HTTP status BEFORE reading headers: a 429/non-200
            # (a CDN/proxy page) carries a real status and must map to
            # RateLimited / SlackHTTPError, never a silently empty scope set.
            if status == 429:
                retry_after = _parse_retry_after(getattr(resp, "headers", None))
                attempts += 1
                if attempts > MAX_RATE_LIMIT_RETRIES:
                    exc2 = RateLimited("auth_test: rate limited")
                    exc2.retry_after_seconds = retry_after
                    exc2.method = "auth_test"
                    raise exc2
                self._sleep(retry_after)
                continue
            if status != 200:
                exc3 = SlackHTTPError(f"auth_test: HTTP {status}")
                exc3.status = status
                raise exc3
            data = getattr(resp, "data", resp)
            if isinstance(data, Mapping) and not data.get("ok", False):
                error = str(data.get("error", ""))
                if error in _RATE_LIMITED_ERRORS:
                    retry_after = _parse_retry_after(getattr(resp, "headers", None))
                    attempts += 1
                    if attempts > MAX_RATE_LIMIT_RETRIES:
                        exc2 = RateLimited("auth_test: rate limited")
                        exc2.retry_after_seconds = retry_after
                        exc2.method = "auth_test"
                        raise exc2
                    self._sleep(retry_after)
                    continue
                raise _error_for(error)
            headers = getattr(resp, "headers", None) or {}
            raw = headers.get("x-oauth-scopes")
            if raw is None:
                # Header names are case-insensitive (RFC 9110 section 5.1): slack_sdk
                # keeps the server's casing in a plain dict.
                try:
                    raw = next(
                        (v for k, v in headers.items()
                         if str(k).lower() == "x-oauth-scopes"),
                        "",
                    )
                except (AttributeError, TypeError):
                    raw = ""
            raw = str(raw or "")
            return frozenset(s.strip() for s in raw.split(",") if s.strip())

    def history(
        self, channel: str, oldest: SlackTs, latest: SlackTs | None = None
    ) -> list[RawMessage]:
        return self._paged(
            "conversations_history", item_key="messages", label="history",
            required=True, channel=channel, oldest=oldest, latest=latest,
            inclusive=True, include_all_metadata=True, limit=HISTORY_PAGE_SIZE,
        )

    def reactions_get(self, channel: str, ts: SlackTs) -> RawMessage:
        data = self._call(
            "reactions_get", label="reactions_get", channel=channel, timestamp=ts, full=True
        )
        return _require(data, "message")

    def reactions_add(self, channel: str, ts: SlackTs, name: str) -> None:
        self._add_bucket.wait(self._sleep)
        self._call(
            "reactions_add", label="reactions_add", channel=channel, timestamp=ts, name=name
        )

    def reactions_remove(self, channel: str, ts: SlackTs, name: str) -> None:
        self._remove_bucket.wait(self._sleep)
        self._call(
            "reactions_remove", label="reactions_remove", channel=channel, timestamp=ts, name=name
        )

    def post_message(
        self, channel: str, *, text: str, blocks: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> SlackTs:
        data = self._call(
            "chat_postMessage", label="post_message",
            channel=channel, text=text, blocks=blocks, metadata=metadata,
        )
        return str(_require(data, "ts"))

    def update_message(
        self, channel: str, ts: SlackTs, *, text: str, blocks: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> SlackTs:
        data = self._call(
            "chat_update", label="update_message",
            channel=channel, ts=ts, text=text, blocks=blocks, metadata=metadata,
        )
        return str(_require(data, "ts"))

    def users_list(self) -> list[RawUser]:
        return self._paged(
            "users_list", item_key="members", required=True, limit=HISTORY_PAGE_SIZE
        )

    def channel_info(self, channel: str) -> RawChannel:
        data = self._call("conversations_info", label="channel_info", channel=channel)
        return _require(data, "channel")

    def conversations_members(self, channel: str) -> list[str]:
        return self._paged(
            "conversations_members", item_key="members", required=True, channel=channel,
            limit=HISTORY_PAGE_SIZE,
        )

    def fetch_file_bytes(self, url: str) -> bytes:
        # The bearer token is an unredirected header: urllib's redirect handler
        # never copies it onto a redirected request, so it never leaves the host
        # the private rendition URL names. A non-https URL is refused before any
        # request is made, so the token never crosses the wire in clear.
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise SlackTransportError("fetch_file_bytes: rendition URL is not https")
        # A raw non-ASCII path cannot be encoded onto the request line; quote it
        # (an already-encoded URL is left unchanged). A URL urllib still rejects
        # surfaces as SlackTransportError, never as a non-SlackError.
        try:
            request = urllib.request.Request(
                urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
            )
        except ValueError as exc:
            raise SlackTransportError("fetch_file_bytes: unusable rendition URL") from exc
        request.add_unredirected_header("Authorization", f"Bearer {self._bot_token}")
        attempts = 0
        while True:
            try:
                response = urllib.request.urlopen(
                    request, timeout=self._fetch_timeout_seconds
                )
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = _parse_retry_after(exc.headers)
                    attempts += 1
                    if attempts > MAX_RATE_LIMIT_RETRIES:
                        exc2 = RateLimited("fetch_file_bytes: rate limited")
                        exc2.retry_after_seconds = retry_after
                        exc2.method = "fetch_file_bytes"
                        raise exc2 from exc
                    self._sleep(retry_after)
                    continue
                exc3 = SlackHTTPError(f"fetch_file_bytes: HTTP {exc.code}")
                exc3.status = exc.code
                raise exc3 from exc
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException, ValueError) as exc:
                raise SlackTransportError(f"fetch_file_bytes: {exc}") from exc
        chunks: list[bytes] = []
        total = 0
        try:
            with response:
                while True:
                    chunk = response.read(65_536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._max_image_bytes:
                        exc4 = FileTooLarge("fetch_file_bytes: rendition exceeded the cap")
                        exc4.limit_bytes = self._max_image_bytes
                        raise exc4
                    chunks.append(chunk)
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException, ValueError) as exc:
            raise SlackTransportError(f"fetch_file_bytes: {exc}") from exc
        return b"".join(chunks)


def make_client(
    token: str,
    *,
    fetch_timeout_seconds: int = 10,
    max_image_bytes: int = 25_000_000,
) -> SlackIO:
    """The only way the rest of the package builds a real client (10 section 2).
    Reads no environment itself: the CLI reads `SLACK_BOT_TOKEN` and passes it in.

    `fetch_timeout_seconds`/`max_image_bytes` default to `FacesConfig`'s own
    documented defaults (40 section 1.10) but are threaded through so a caller
    with a loaded config (the CLI) can pass its configured values.
    """
    from slack_sdk import WebClient

    return SlackWebClient(
        # No slack_sdk built-in retries: its connection-error handler would
        # silently re-send a post that may already have landed (10 section 3).
        # Rate limits are handled by SlackWebClient itself.
        WebClient(token=token, retry_handlers=[]),
        bot_token=token,
        fetch_timeout_seconds=fetch_timeout_seconds,
        max_image_bytes=max_image_bytes,
    )
