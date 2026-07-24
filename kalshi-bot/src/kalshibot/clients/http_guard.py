"""Funding/transfer denylist guard (spec constraint 1.1 -- non-negotiable).

The bot must NEVER move money into or out of the account. This guard is the
hard enforcement of that: every outbound request path is checked, and any path
that looks like it touches funding, transfers, banking, deposits, or
withdrawals raises immediately. It is a hard failure, not a warning, and it
lives in the HTTP layer so it cannot be bypassed by a higher-level caller.

This is intentionally over-broad. If a legitimate read is ever caught by it,
the correct fix is to narrow the pattern deliberately and document why -- never
to route around the guard.
"""
from __future__ import annotations

import re

# Substrings / patterns that, if present in a request path, indicate a
# money-movement endpoint. Matched case-insensitively against the URL path.
_DENY_PATTERNS = [
    re.compile(r"deposit", re.IGNORECASE),
    re.compile(r"withdraw", re.IGNORECASE),
    re.compile(r"transfer", re.IGNORECASE),
    re.compile(r"\bach\b", re.IGNORECASE),
    re.compile(r"wire", re.IGNORECASE),
    re.compile(r"bank", re.IGNORECASE),
    re.compile(r"funding", re.IGNORECASE),
    re.compile(r"\bfund\b", re.IGNORECASE),
    re.compile(r"payment", re.IGNORECASE),
    re.compile(r"debit", re.IGNORECASE),
    re.compile(r"card", re.IGNORECASE),
    re.compile(r"cashout", re.IGNORECASE),
    re.compile(r"payout", re.IGNORECASE),
]


class FundingOperationBlocked(RuntimeError):
    """Raised when a request path matches the funding/transfer denylist."""


def assert_path_allowed(path: str) -> None:
    """Raise FundingOperationBlocked if `path` looks like a money-movement call.

    Call this on every outbound request path before it is sent.
    """
    for pat in _DENY_PATTERNS:
        if pat.search(path):
            raise FundingOperationBlocked(
                f"Blocked request to funding/transfer-related path (matched "
                f"/{pat.pattern}/): {path!r}. Moving money is permanently out of "
                f"scope; the account owner does this manually via Kalshi's own UI."
            )
