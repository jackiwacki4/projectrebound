"""The HTML dashboard.

It is a static file opened from disk -- no server, no port, no login. These tests
pin the properties that keep it that way and keep it honest: nothing loaded over
the network, the sample-size warning present while the sample is small, and profit
and loss never signalled by colour alone (status green vs status red measure CVD
deltaE 4.1, i.e. indistinguishable to a red-green colourblind reader).
"""
import re

from kalshibot.reporting.dashboard import generate_dashboard


def _seed_market(dao, ticker, *, price, result, gate_passed, ts, close, prob=0.7,
                 spread=1, polls=3):
    dao.upsert_market(ticker=ticker, series="S", city=None, title="t",
                      close_ts=close, status="settled")
    for i in range(polls):
        dao.insert_decision(
            decision_ts=ts + i * 60_000, ticker=ticker, model_name="m", probability=prob,
            uncertainty=1.0, inputs={}, book_snapshot_id=None, best_yes_bid=price - 1,
            best_yes_ask=price, intended_side="yes", intended_price_cents=price,
            edge_after_fees=0.08, spread_cents=spread, depth_at_price=500,
            gate_passed=gate_passed and i == 0,
            blocked_by=None if (gate_passed and i == 0) else "min_edge")
    dao.upsert_settlement(ticker, result, close)


def _html(dao, family="sports", db_path="./data/x.db"):
    return generate_dashboard(dao, family, db_path)


def test_page_loads_nothing_from_the_network(dao):
    """It has to work offline, from a file:// URL, forever. One CDN reference
    would make the page silently degrade the first time it is opened on a plane."""
    _seed_market(dao, "MKT-A", price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000)
    h = _html(dao)
    assert not re.search(r'(src|href)="(?!#)(https?:)?//', h)
    assert "<style>" in h and "<script>" in h        # inlined, not linked


def test_warns_while_the_sample_is_too_small(dao):
    _seed_market(dao, "MKT-A", price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000)
    h = _html(dao)
    assert "Too early to conclude anything" in h
    assert "1 settled markets" in h
    # And it must say plainly that the row count is not the sample size.
    assert "NOT the sample size" in h


def test_profit_and_loss_never_relies_on_colour(dao):
    """Status green and status red are CVD-indistinguishable, so every signed
    number carries a direction glyph and a word as well."""
    _seed_market(dao, "WIN", price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000)
    _seed_market(dao, "LOSS", price=80, result="no", gate_passed=True,
                 ts=1_100_000, close=2_100_000)
    h = _html(dao)
    assert "&#9650; up" in h        # up triangle + the word
    assert "&#9660; down" in h      # down triangle + the word
    # Colour is present too, but only alongside the words.
    for cls in ('class="pos"', 'class="neg"'):
        assert cls in h


def test_says_nothing_traded_rather_than_showing_an_empty_chart(dao):
    """The sports family's real result: no market cleared the gates. That must
    read as the gates working, not as a broken page."""
    _seed_market(dao, "MKT-A", price=50, result="yes", gate_passed=False,
                 ts=1_000_000, close=2_000_000)
    h = _html(dao)
    assert "No market has cleared every risk gate yet" in h
    assert "zero trades is the" in h
    assert "no trades, so no result" in h


def test_entries_are_one_per_market_not_one_per_poll(dao):
    """The same market polled 300 times is one entry, not 300 -- one table row
    and one point on the P&L line. (The ticker also appears in that point's two
    tooltip targets, the visible dot and its larger hit area, so counting bare
    occurrences of the ticker would be counting the wrong thing.)"""
    _seed_market(dao, "MKT-A", price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000, polls=300)
    h = _html(dao)
    assert h.count("<td>MKT-A</td>") == 1        # exactly one row in the table
    assert h.count("running total") == 2         # one plotted point (dot + hit area)
    assert "entry 1" in h and "entry 300" not in h


def test_charts_carry_hover_tooltips_and_accessible_labels(dao):
    _seed_market(dao, "MKT-A", price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000)
    h = _html(dao)
    assert h.count("<svg") == 3
    assert h.count('role="img"') == 3      # each chart is labelled for screen readers
    assert "data-tip=" in h                # and hoverable
    assert "<table" in h                   # table view of the same numbers


def test_dark_mode_is_declared_not_left_to_the_browser(dao):
    _seed_market(dao, "MKT-A", price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000)
    h = _html(dao)
    assert "prefers-color-scheme: dark" in h
    assert "--series-1: #3987e5" in h      # the dark step, not the light one flipped


def test_survives_an_empty_database(dao):
    h = _html(dao)
    assert "no price history yet" in h
    assert "<html" in h and "</html>" in h


def test_ticker_is_escaped(dao):
    """Tickers come from an API response; they land in HTML."""
    _seed_market(dao, '<script>x</script>', price=30, result="yes", gate_passed=True,
                 ts=1_000_000, close=2_000_000)
    h = _html(dao)
    assert "<script>x</script>" not in h.split("<script>")[-1]
    assert "&lt;script&gt;" in h
