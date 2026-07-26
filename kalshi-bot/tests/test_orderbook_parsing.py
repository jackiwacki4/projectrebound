"""Order-book parsing, against the REAL Kalshi response shape.

The live API returns:
    {"orderbook_fp": {"yes_dollars": [["0.0100","3166.32"], ...],
                      "no_dollars":  [["0.0100","2276.30"], ...]}}

Not the {"orderbook": {"yes": [[cents, count]]}} shape originally assumed. That
mismatch produced an EMPTY book on every poll -- 8,540 consecutive empty
snapshots in the first overnight run, with no error, no warning, and therefore
zero decisions. These tests exist so that never recurs silently.
"""
from kalshibot.clients.kalshi_client import KalshiClient

# Verbatim excerpt from a live KXHIGHCHI-26JUL26-T89 response.
REAL_FP = {
    "yes_dollars": [["0.0100", "3166.32"], ["0.0200", "578.00"], ["0.1400", "26.00"]],
    "no_dollars": [["0.0100", "2276.30"], ["0.8300", "12.00"], ["0.0300", "99.00"]],
}


def test_dollar_strings_convert_to_cents():
    levels = KalshiClient._parse_levels(REAL_FP["yes_dollars"], dollars=True)
    prices = [p for p, _ in levels]
    assert 14 in prices and 2 in prices and 1 in prices   # "0.1400" -> 14c
    assert max(prices) == 14


def test_levels_sorted_best_bid_first():
    """These are bids: best is the HIGHEST price. The API returns ascending."""
    levels = KalshiClient._parse_levels(REAL_FP["no_dollars"], dollars=True)
    assert [p for p, _ in levels] == [83, 3, 1]


def test_fractional_counts_do_not_crash():
    """Counts arrive as decimal strings ("3166.32"); int() on that raises."""
    levels = KalshiClient._parse_levels([["0.0100", "3166.32"]], dollars=True)
    assert levels == [[1, 3166]]


def test_malformed_levels_are_skipped_not_fatal():
    levels = KalshiClient._parse_levels(
        [["0.1400", "26.00"], ["oops", "5"], [], ["0.0200", None]], dollars=True)
    assert levels == [[14, 26]]


def test_empty_and_missing_sides():
    assert KalshiClient._parse_levels(None, dollars=True) == []
    assert KalshiClient._parse_levels([], dollars=True) == []


def test_legacy_integer_cent_shape_still_supported():
    levels = KalshiClient._parse_levels([[45, 100], [50, 20]], dollars=False)
    assert levels == [[50, 20], [45, 100]]


def test_full_parse_produces_a_usable_book(monkeypatch):
    """End-to-end through get_orderbook: real shape must yield real quotes."""
    client = KalshiClient.__new__(KalshiClient)   # bypass __init__ (needs a key)
    monkeypatch.setattr(KalshiClient, "_request",
                        lambda self, m, e, b=None: {"orderbook_fp": REAL_FP})
    ob = client.get_orderbook("KXHIGHCHI-26JUL26-T89")
    assert ob.yes_levels and ob.no_levels          # NOT empty -- the original bug
    assert ob.yes_levels[0] == [14, 26]            # best YES bid 14c
    assert ob.no_levels[0] == [83, 12]             # best NO bid 83c
    # Implied YES ask = 100 - best NO bid = 17c, giving a sane 3c spread.
    assert 100 - ob.no_levels[0][0] == 17
