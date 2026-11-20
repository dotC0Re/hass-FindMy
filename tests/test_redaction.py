"""Tests for what keeps Anisette credentials and emulator secrets out of the log.

Review findings A-2 (the ``anisette`` package logs provisioning secrets at debug level) and
A-3 (the credentials of a remote Anisette URL in error texts and in FindMy.py's own log lines).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path

import aiohttp
import pytest
from yarl import URL

import custom_components.findmy as integration
from custom_components.findmy import config_flow, coordinator, redaction
from custom_components.findmy.redaction import (
    REDACTED,
    describe_error,
    protect_logs,
    redact_url_credentials,
    url_without_credentials,
)

MANIFEST = Path(integration.__file__).with_name("manifest.json")
SECRET = "s3cret"


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("no url here, just user@example.com", "no url here, just user@example.com"),
        ("http://ani.example:6969/v3", "http://ani.example:6969/v3"),
        ("https://user:s3cret@ani.example/v3", f"https://{REDACTED}@ani.example/v3"),
        ("https://token@ani.example", f"https://{REDACTED}@ani.example"),
        # An unencoded `@` in the password: everything up to the last one goes.
        ("https://user:p@s3cret@ani.example/v3", f"https://{REDACTED}@ani.example/v3"),
        # An `@` further on belongs to the path or the query, not to the credentials.
        ("https://ani.example/v3?mail=a@b.c", "https://ani.example/v3?mail=a@b.c"),
        ("https://ani.example/a@b", "https://ani.example/a@b"),
        (
            "Cannot connect to https://user:s3cret@a.example:1/x, url='http://u:s3cret@b.example/'",
            f"Cannot connect to https://{REDACTED}@a.example:1/x, url='http://{REDACTED}@b.example/'",
        ),
    ],
)
def test_credentials_of_urls_in_a_text_are_redacted(text: str, redacted: str) -> None:
    assert redact_url_credentials(text) == redacted


@pytest.mark.parametrize(
    ("url", "logged"),
    [
        ("http://ani.example:6969", "http://ani.example:6969"),
        ("https://user:s3cret@ani.example:6969/v3", f"https://{REDACTED}@ani.example:6969/v3"),
        ("https://user:p@s3cret@ani.example", f"https://{REDACTED}@ani.example"),
        # Not a URL that can be taken apart: nothing of it is logged.
        ("https://user:pa/s3cret@ani.example", REDACTED),
        ("https://user:pa#s3cret@ani.example", REDACTED),
        ("https://user:pa?s3cret@ani.example", REDACTED),
    ],
)
def test_url_is_logged_without_credentials(url: str, logged: str) -> None:
    assert url_without_credentials(url) == logged


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pa/s3cret@ani.example/v3",
        "https://user:pa#s3cret@ani.example/v3",
        "https://user:pa?s3cret@ani.example/v3",
        "https://ani.example:port/v3",
    ],
)
def test_url_whose_credentials_cannot_be_recognised_is_refused(url: str) -> None:
    """Such a URL would fail at the first request and be logged with the password."""
    assert config_flow._is_valid_anisette_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://ani.example:6969",
        "https://user:s3cret@ani.example:6969/v3",
        "https://user:p@s3cret@ani.example",
        "https://user:pa%2Fs3cret@ani.example/v3",
    ],
)
def test_usual_urls_are_accepted(url: str) -> None:
    assert config_flow._is_valid_anisette_url(url) is True


def response_error(url: str) -> aiohttp.ClientResponseError:
    """What aiohttp raises for an error status; its text quotes the request URL."""
    request = aiohttp.RequestInfo(URL(url), "GET", {}, URL(url))  # pyright: ignore[reportArgumentType]
    return aiohttp.ClientResponseError(request, (), status=503, message="Service Unavailable")


def test_error_text_of_a_fetch_carries_no_credentials() -> None:
    """Home Assistant logs this text at error level ("Error fetching ... data")."""
    err = response_error(f"https://user:{SECRET}@ani.example/v3/client_info")
    assert SECRET in str(err)

    message = coordinator._describe_fetch_error(err)

    assert SECRET not in message
    assert message.startswith("Could not reach Apple: ")
    assert f"https://{REDACTED}@ani.example/v3/client_info" in message
    assert SECRET not in describe_error(err)


@pytest.fixture
def record_factory() -> Generator[logging.Logger]:
    """A logger of FindMy.py that the integration protects, with the level opened."""
    logger = logging.getLogger("findmy.util.http")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    yield logger
    logger.setLevel(previous)


def test_log_lines_of_the_library_are_redacted(
    record_factory: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    """FindMy.py logs the URL before each request and the error text when it retries."""
    url = f"https://user:{SECRET}@ani.example/v3"
    with caplog.at_level(logging.DEBUG):
        record_factory.warning(
            "Error while making HTTP request; retrying after %i seconds. %s",
            5,
            response_error(url),
        )
        logging.getLogger("findmy.reports.anisette").info("Fetching anisette data from %s", url)

    assert len(caplog.records) == 2
    assert SECRET not in caplog.text
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert f"Fetching anisette data from https://{REDACTED}@ani.example/v3" in caplog.text
    assert "retrying after 5 seconds" in caplog.text


def test_traceback_of_a_logged_exception_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("custom_components.findmy.coordinator")
    error = response_error(f"https://user:{SECRET}@ani.example/v3")
    try:
        raise error
    except aiohttp.ClientResponseError:
        with caplog.at_level(logging.DEBUG):
            logger.debug("Fetching location reports failed", exc_info=True)

    (record,) = caplog.records
    assert SECRET not in caplog.text
    assert record.exc_text is not None
    assert "ClientResponseError" in record.exc_text
    assert REDACTED in record.exc_text


def test_records_without_credentials_are_left_as_they_are(
    record_factory: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        record_factory.info("Fetched %i reports from %s", 3, "https://gateway.example/acsnservice")

    (record,) = caplog.records
    # Text and arguments untouched, so that handlers which group by the text still can.
    assert record.msg == "Fetched %i reports from %s"
    assert record.args == (3, "https://gateway.example/acsnservice")


def test_record_that_cannot_be_formatted_is_passed_on(record_factory: logging.Logger) -> None:
    record = record_factory.makeRecord(
        record_factory.name, logging.INFO, __file__, 1, "%i items", ("many",), None
    )

    assert redaction._FILTER.filter(record) is True


def test_every_logger_of_the_integration_and_the_library_is_protected() -> None:
    package = integration.__name__
    names = {
        package,
        # Modules that are imported after the package, or not at all in a test run.
        f"{package}.config_flow",
        f"{package}.coordinator",
        f"{package}.device_tracker",
        "findmy",
        "findmy.util.http",
        "findmy.reports.anisette",
        "findmy.reports.account",
    }
    for name in names:
        assert redaction._FILTER in logging.getLogger(name).filters, name


def test_protecting_again_adds_nothing() -> None:
    protect_logs(integration.__name__, list(integration.__path__))
    protect_logs(integration.__name__, list(integration.__path__))

    filters = logging.getLogger("findmy.util.http").filters
    assert filters.count(redaction._FILTER) == 1


def test_debug_logging_does_not_reach_the_anisette_package() -> None:
    """Its debug lines dump the provisioning state of the emulated device as hex."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # "Enable debug logging" on the integration page sets the loggers named here.
    assert manifest["loggers"] == ["findmy"]

    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        # What `logger: default: debug` does.
        assert logging.getLogger("anisette._adi").isEnabledFor(logging.DEBUG) is False
        assert logging.getLogger("anisette._fs").isEnabledFor(logging.DEBUG) is False
        assert logging.getLogger("anisette._adi").isEnabledFor(logging.INFO) is True
    finally:
        root.setLevel(previous)


def test_level_somebody_set_for_the_anisette_package_is_kept() -> None:
    logger = logging.getLogger("anisette")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        protect_logs(integration.__name__, list(integration.__path__))
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(previous)
