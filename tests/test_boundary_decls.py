"""Tests for the slack_io and faces boundary declarations."""

from __future__ import annotations

import hashlib

import pytest

from snipebot import faces, slack_io


def test_error_hierarchy_bases():
    assert issubclass(slack_io.SlackTransportError, slack_io.SlackError)
    assert issubclass(slack_io.SlackHTTPError, slack_io.SlackError)
    assert issubclass(slack_io.RateLimited, slack_io.SlackError)
    assert issubclass(slack_io.SlackPaginationError, slack_io.SlackError)
    assert issubclass(slack_io.SlackAPIError, slack_io.SlackError)
    assert issubclass(slack_io.FileTooLarge, slack_io.SlackError)


def test_error_hierarchy_tolerated_subclass_api_error():
    assert issubclass(slack_io.AlreadyReacted, slack_io.SlackAPIError)
    assert issubclass(slack_io.NoReaction, slack_io.SlackAPIError)
    assert issubclass(slack_io.MessageNotFound, slack_io.SlackAPIError)


def test_error_hierarchy_fatal_subclass_api_error():
    assert issubclass(slack_io.NotInChannel, slack_io.SlackAPIError)
    assert issubclass(slack_io.ChannelNotFound, slack_io.SlackAPIError)
    assert issubclass(slack_io.MissingScope, slack_io.SlackAPIError)
    assert issubclass(slack_io.AuthError, slack_io.SlackAPIError)


def test_error_classes_are_not_cross_related():
    # Tolerated classes are siblings, not subclasses of each other.
    assert not issubclass(slack_io.AlreadyReacted, slack_io.NoReaction)
    assert not issubclass(slack_io.NotInChannel, slack_io.ChannelNotFound)


def test_auth_identity_is_frozen_dataclass():
    identity = slack_io.AuthIdentity(
        user_id="U1", bot_id="B1", team_id="T1", url="https://example.slack.com/"
    )
    with pytest.raises(Exception):
        identity.user_id = "U2"  # type: ignore[misc]


def test_constants_present():
    assert slack_io.HISTORY_PAGE_SIZE == 200
    assert slack_io.MAX_PAGINATION_ITERATIONS == 10_000
    assert slack_io.MAX_RATE_LIMIT_RETRIES == 8
    assert slack_io.REACTIONS_ADD_PER_MINUTE == 50
    assert slack_io.REACTIONS_REMOVE_PER_MINUTE == 20


def test_fake_face_detector_returns_mapped_count():
    payload = b"some-fixture-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    detector = faces.FakeFaceDetector({digest: 3})
    assert detector.count_faces(payload) == 3


def test_fake_face_detector_raises_on_unknown_hash():
    detector = faces.FakeFaceDetector({})
    with pytest.raises(KeyError):
        detector.count_faces(b"unregistered-bytes")


def test_undecodable_image_is_exception():
    assert issubclass(faces.UndecodableImage, Exception)


