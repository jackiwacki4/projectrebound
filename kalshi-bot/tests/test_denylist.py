"""Funding/transfer denylist guard tests (spec constraint 1.1).

Asserts the guard hard-fails on every money-movement path shape, and that the
Kalshi client routes its requests through the guard (so no funding endpoint can
be reached even if one were added by mistake).
"""
import pytest

from kalshibot.clients.http_guard import FundingOperationBlocked, assert_path_allowed


@pytest.mark.parametrize("path", [
    "/trade-api/v2/portfolio/deposit",
    "/trade-api/v2/portfolio/withdraw",
    "/trade-api/v2/portfolio/withdrawals",
    "/trade-api/v2/transfers",
    "/trade-api/v2/ach/link",
    "/trade-api/v2/wire/instructions",
    "/trade-api/v2/bank/accounts",
    "/trade-api/v2/funding/status",
    "/trade-api/v2/payments/methods",
    "/trade-api/v2/debit-card/add",
    "/trade-api/v2/cashout",
    "/trade-api/v2/payout/history",
])
def test_funding_paths_are_blocked(path):
    with pytest.raises(FundingOperationBlocked):
        assert_path_allowed(path)


@pytest.mark.parametrize("path", [
    "/trade-api/v2/markets",
    "/trade-api/v2/markets/KXHIGHCHI-25JUL24/orderbook",
    "/trade-api/v2/portfolio/balance",
    "/trade-api/v2/portfolio/positions",
    "/trade-api/v2/portfolio/orders",
    "/trade-api/v2/events",
])
def test_normal_paths_are_allowed(path):
    assert_path_allowed(path)  # must not raise


def test_client_request_layer_calls_the_guard(monkeypatch, tmp_path):
    """Even a mistaken funding call routed through the client is blocked before
    any network I/O happens."""
    from kalshibot.clients import kalshi_client as kc

    # Build a client without a real key by stubbing key loading + signing.
    monkeypatch.setattr(kc.KalshiClient, "_load_key", staticmethod(lambda path: object()))
    monkeypatch.setattr(kc.KalshiClient, "_sign", lambda self, ts, m, p: "sig")

    from kalshibot.config import Credentials
    creds = Credentials(api_key_id="k", private_key_path="x", environment="demo")
    client = kc.KalshiClient(creds)

    # If the guard ever failed to fire, urlopen would be reached; make that loud.
    def explode(*a, **k):
        raise AssertionError("network reached -- guard did not block funding path")
    monkeypatch.setattr(kc.urllib.request, "urlopen", explode)

    with pytest.raises(FundingOperationBlocked):
        client._request("POST", "/portfolio/withdraw", {"amount": 100})
