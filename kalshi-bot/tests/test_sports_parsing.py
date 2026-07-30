"""Sports market parsing, against REAL Kalshi payloads.

Every fixture below is a verbatim field-set captured from the live Kalshi API
(KXMLBGAME / KXNFLGAME, 2026-07). Four traps these tests exist to prevent
regressing:

1. Both `yes_sub_title` and `no_sub_title` carry the SAME team name on live
   sports markets, so the two sides of a game are indistinguishable from them.
   Only the ticker suffix says which team YES pays out on.
2. MLB tickers carry a 4-digit Eastern start time and NFL tickers do not. A
   parser that assumes either shape silently drops a whole league.
3. Team codes are variable length and concatenated (SEA+LAD, AZ+WSH), so the
   pair segment cannot be split into home/away -- that has to come from the
   linked schedule, not from string surgery.
4. Anything that is not a game-winner market (totals, futures) must be declined
   rather than half-understood.
"""
import pytest

from kalshibot.config import LeagueConfig
from kalshibot.models.sports import parse_game_market, parse_sports_ticker
from kalshibot.util import iso_to_ms

MLB = LeagueConfig(name="mlb", kalshi_series="KXMLBGAME", espn_path="baseball/mlb",
                   margin_sigma=4.4, home_advantage_margin=0.25, regulation_periods=9)
NFL = LeagueConfig(name="nfl", kalshi_series="KXNFLGAME", espn_path="football/nfl",
                   margin_sigma=13.5, home_advantage_margin=1.5, regulation_periods=4)

# Verbatim from the live API. Note yes_sub_title == no_sub_title.
MLB_MARKET = {
    "ticker": "KXMLBGAME-26JUL282210SEALAD-SEA",
    "event_ticker": "KXMLBGAME-26JUL282210SEALAD",
    "title": "Seattle vs Los Angeles D Winner?",
    "yes_sub_title": "Seattle",
    "no_sub_title": "Seattle",
    "strike_type": "structured",
    "status": "active",
    "rules_primary": ("If Seattle wins the Seattle vs Los Angeles D professional "
                      "baseball game originally scheduled for Jul 28, 2026 at 10:10 PM EDT, "
                      "then the market resolves to Yes."),
}
MLB_MARKET_OTHER_SIDE = {
    "ticker": "KXMLBGAME-26JUL282210SEALAD-LAD",
    "event_ticker": "KXMLBGAME-26JUL282210SEALAD",
    "title": "Seattle vs Los Angeles D Winner?",
    "yes_sub_title": "Los Angeles D",
    "no_sub_title": "Los Angeles D",
}
NFL_MARKET = {
    "ticker": "KXNFLGAME-26AUG15DALSEA-SEA",
    "event_ticker": "KXNFLGAME-26AUG15DALSEA",
    "title": "Will Seattle win the Dallas vs Seattle Pro Football game?",
}


def test_yes_team_comes_from_the_ticker_not_the_subtitles():
    """The subtitles are identical on both sides; only the suffix distinguishes them."""
    assert MLB_MARKET["yes_sub_title"] == MLB_MARKET["no_sub_title"]
    a = parse_game_market(MLB_MARKET, MLB)
    b = parse_game_market(MLB_MARKET_OTHER_SIDE, MLB)
    assert a.yes_team == "SEA"
    assert b.yes_team == "LAD"
    # Both sides of one game share the join key used by every sports table.
    assert a.game_key == b.game_key == "KXMLBGAME-26JUL282210SEALAD"


def test_mlb_ticker_carries_an_eastern_start_time():
    m = parse_game_market(MLB_MARKET, MLB)
    assert m.target_date == "2026-07-28"
    # 10:10 PM EDT on Jul 28 is 02:10Z on Jul 29 -- matching the rules text.
    assert m.start_ts == iso_to_ms("2026-07-29T02:10:00Z")


def test_nfl_ticker_has_no_time_and_still_parses():
    m = parse_game_market(NFL_MARKET, NFL)
    assert (m.target_date, m.pair_code, m.yes_team) == ("2026-08-15", "DALSEA", "SEA")
    assert m.start_ts is None      # no time in the ticker; the schedule supplies it
    assert m.league == "nfl"


def test_two_letter_and_three_letter_codes_both_survive():
    """Kalshi codes are 2-4 characters (SD, TB, AZ, CWS, WSH), concatenated."""
    parts = parse_sports_ticker("KXMLBGAME-26JUL261335AZWSH-AZ")
    assert (parts.pair_code, parts.yes_team) == ("AZWSH", "AZ")
    parts = parse_sports_ticker("KXMLBGAME-26JUL261215CLETB-TB")
    assert (parts.pair_code, parts.yes_team) == ("CLETB", "TB")


@pytest.mark.parametrize("ticker", [
    "KXMLBTOTAL-26JUL261410ATHMIN-9",      # a totals market, not a winner market
    "KXHIGHCHI-26JUL26-T96",               # a weather market
    "KXMLBGAME-26XXX282210SEALAD-SEA",     # unparseable month
    "KXMLBGAME-26JUL282560SEALAD-SEA",      # impossible time (25:60)
    "KXMLBGAME-26JUL282210SEALAD-BOS",     # YES team not in the matchup
    "KXMLBGAME-26FEB302210SEALAD-SEA",     # date that does not exist
    "",
])
def test_declines_anything_that_is_not_a_game_winner_market(ticker):
    assert parse_sports_ticker(ticker) is None
    assert parse_game_market({"ticker": ticker}, MLB) is None


def test_market_without_a_ticker_is_declined():
    assert parse_game_market({"title": "Seattle vs Los Angeles D Winner?"}, MLB) is None


def test_game_key_falls_back_to_the_ticker_stem_when_event_ticker_is_absent():
    m = parse_game_market({"ticker": "KXMLBGAME-26JUL282210SEALAD-SEA"}, MLB)
    assert m.game_key == "KXMLBGAME-26JUL282210SEALAD"
