"""Keep the credentials of an Anisette URL and the secrets of the emulator out of the log.

Why
---
Logs end up in public issues. Two things this integration handles must not get there:

* The URL of a private Anisette server may carry ``user:password@``. aiohttp quotes the request
  URL in the text of its errors, and FindMy.py logs the URL itself (at info level before every
  request, at warning level with the error when it retries).
* The integrated Anisette provider runs the ``anisette`` package, whose debug lines dump the
  provisioning state of the emulated device as hex: the persistent token, the trust key and
  every write to ``adi.pb``. Together with the device ids that is the trusted device.

What this module does
---------------------
* :func:`redact_url_credentials`, :func:`describe_error` and :func:`url_without_credentials`
  are what the integration's own code passes error texts and URLs through before they are
  logged or shown.
* :func:`protect_logs` is called once, when the integration is imported. It puts a filter on
  the loggers of FindMy.py and of this integration that redacts the credentials in every
  record, including the traceback of an attached exception, and it keeps the ``anisette``
  package at warning level, which is Home Assistant's default (its info lines only report
  progress, such as "Provisioning..."). The manifest does not list ``anisette`` under
  ``loggers`` either, so "Enable debug logging" on the integration page does not switch it on.

Limits
------
* A filter belongs to one logger and is not inherited, so the loggers have to be known by
  name: the ones that exist when the integration is imported, the modules of this package and
  the modules of FindMy.py that make requests. A module FindMy.py adds later is not covered
  until it is listed here.
* Somebody who sets the level of the ``anisette`` logger to debug by name, in the ``logger``
  configuration or with its service, gets these lines. That is a decision of its own and is
  left alone; only the general switches (the default level, the button on the integration
  page) no longer reach it.
* Credentials are found by the shape ``scheme://userinfo@``, where the user info has no
  ``/``, ``?``, ``#``, quote, angle bracket or white space: in a log line, these are where a
  URL ends. A URL whose credentials contain one of them unencoded is refused when it is
  entered (:func:`credentials_are_recognised`, used by the config flow), so it never gets as
  far as a request. An entry created before this check existed keeps its URL until the account
  signs in again.
"""

from __future__ import annotations

import logging
import pkgutil
import re
import traceback
from typing import Final, final, override
from urllib.parse import urlsplit

REDACTED: Final = "**REDACTED**"

# `scheme://user:password@host` wherever it appears in a text. Greedy up to the last `@` before
# the host ends, because a password may contain an unencoded `@`.
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/?#\s'\"<>]*@")

# The modules of FindMy.py that make requests or log what they sent. Loggers that exist when
# protect_logs runs are covered anyway; these are named in case one is imported later.
_LIBRARY_LOGGERS = (
    "findmy",
    "findmy.reports.account",
    "findmy.reports.anisette",
    "findmy.reports.reports",
    "findmy.util.http",
)
_ANISETTE_LOGGER = "anisette"


def redact_url_credentials(text: str) -> str:
    """Replace the ``user:password`` of every URL in a text."""
    return _URL_CREDENTIALS.sub(f"{REDACTED}@", text)


def credentials_are_recognised(url: str) -> bool:
    """Whether the redaction finds the credentials of this URL, or it has none.

    Raises ValueError for a URL that cannot be taken apart.
    """
    parsed = urlsplit(url)
    if parsed.username is None and parsed.password is None:
        return True
    # What must be left of the host part once the credentials are gone.
    host = parsed.netloc.rpartition("@")[2]
    return f"://{REDACTED}@{host}" in redact_url_credentials(url)


def describe_error(err: BaseException) -> str:
    """An error for the log, without the credentials of a URL it may quote."""
    return redact_url_credentials(f"{type(err).__name__}: {err}")


def url_without_credentials(url: str) -> str:
    """Drop ``user:password@`` from a URL before it is logged."""
    try:
        parsed = urlsplit(url)
        # Reading the port raises ValueError for one that is not a number. That is what is
        # left of a password with an unencoded `/`, `?` or `#`: the host part ends there and
        # reads as `user:<not a port>`, without any credentials to be found. Nothing of such
        # a URL is logged.
        port = parsed.port
    except ValueError:
        return REDACTED
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname or ""
    netloc = f"{host}:{port}" if port else host
    return parsed._replace(netloc=f"{REDACTED}@{netloc}").geturl()


@final
class _UrlCredentialFilter(logging.Filter):
    """Redacts the credentials of URLs in a log record. Never drops a record."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            # A record whose arguments do not fit its text; logging reports that itself.
            return True
        redacted = redact_url_credentials(message)
        if redacted != message:
            # The arguments are merged into the text: one of them held the URL.
            record.msg, record.args = redacted, None

        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            # Formatters use exc_text if it is set and format the exception otherwise, so the
            # redacted traceback has to be put there before a handler sees the record.
            text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            if redact_url_credentials(text) != text:
                record.exc_text = redact_url_credentials(text)
        return True


_FILTER = _UrlCredentialFilter()


def protect_logs(package: str, package_path: list[str]) -> None:
    """Install the protections described in the module docstring. Safe to call again.

    ``package`` and ``package_path`` are ``__name__`` and ``__path__`` of this integration.
    Lists the package directory, so it must not run on the event loop; Home Assistant imports
    integrations in an executor thread.
    """
    names = {package, *_LIBRARY_LOGGERS}
    names.update(f"{package}.{module.name}" for module in pkgutil.iter_modules(package_path))
    names.update(
        name
        # A copy: creating a logger below adds to the dictionary.
        for name in list(logging.root.manager.loggerDict)
        if name.startswith(("findmy.", f"{package}."))
    )
    for name in names:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)

    anisette = logging.getLogger(_ANISETTE_LOGGER)
    if anisette.level == logging.NOTSET:
        # Its modules inherit this. A level somebody set by name stays as it is.
        anisette.setLevel(logging.WARNING)
