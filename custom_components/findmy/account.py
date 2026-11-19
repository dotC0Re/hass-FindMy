"""Apple account state as it is kept in a config entry.

The FindMy library serialises an account, including the Apple ID password, into one mapping.
Config entries are stored as plain text and end up in every backup, so the password must not
be part of what is written there (finding S1). The session tokens in the mapping are enough to
fetch location reports; once Apple invalidates them the user signs in again through the
re-authentication flow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from findmy import AccountStateMapping


def without_password(account_data: AccountStateMapping) -> AccountStateMapping:
    """Return a copy of serialised account state that does not contain the password.

    The input is left untouched; it may be the read-only data of a config entry.
    """
    return {**account_data, "account": {**account_data["account"], "password": None}}
