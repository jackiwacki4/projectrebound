"""Rating providers and the ESPN feed parsing.

Offline tests. The endpoints and field names themselves were verified against the
live ESPN API (see the docstrings in clients/espn_client.py); the point here is
that the maths each member claims to implement is the maths it actually does, and
that a member with thin data abstains instead of guessing.

The fixtures are trimmed but verbatim-shaped captures from the live API (2026-07),
including the two shapes that are easy to get wrong: the standings tree, where
entries hang off nested children, and `pickcenter`, where the spread is quoted
home-relative.
"""
import pytest

from kalshibot.clients import sports_providers as sp
from kalshibot.clients import espn_client as ec
from kalshibot.clients.espn_client import EspnClient, TeamStanding, to_kalshi_code
from kalshibot.config import LeagueConfig
from kalshibot.models.sports import prob_home_from_margin

MLB = LeagueConfig(name="mlb", kalshi_series="KXMLBGAME", espn_path="baseball/mlb",
                   margin_sigma=4.4, home_advantage_margin=0.25, regulation_periods=9,
                   elo_k=4.0, elo_home_advantage=25.0, elo_min_games=4)

MATCHUP = sp.Matchup(game_key="KXMLBGAME-26JUL282210SEALAD", away_code="SEA",
                     home_code="LAD", start_ts=1, source_event_id="401816999")


def _standing(code, wins, losses, pf, pa):
    return TeamStanding(code=code, wins=wins, losses=losses, points_for=pf,
                        points_against=pa)


def _data(standings=None, completed=None):
    return sp.LeagueData(league=MLB,
                         standings={s.code: s for s in (standings or [])},
                         completed=completed or [])


# --------------------------------------------------------------------------
# Team-code translation
# --------------------------------------------------------------------------
def test_team_codes_are_translated_per_league_not_globally():
    """ESPN's WSH is Kalshi's WSH in baseball but WAS in football. A single flat
    table would mis-link one of the two leagues, silently."""
    assert to_kalshi_code("WSH", "mlb") == "WSH"
    assert to_kalshi_code("WSH", "nfl") == "WAS"
    assert to_kalshi_code("ARI", "mlb") == "AZ"      # Diamondbacks
    assert to_kalshi_code("ARI", "nfl") == "ARI"     # Cardinals
    assert to_kalshi_code("CHW", "mlb") == "CWS"
    assert to_kalshi_code("JAX", "nfl") == "JAC"
    assert to_kalshi_code("SEA", "mlb") == "SEA"     # the common case: unchanged


def test_unlisted_league_falls_back_to_identity():
    assert to_kalshi_code("BOS", "nhl") == "BOS"


# --------------------------------------------------------------------------
# log5
# --------------------------------------------------------------------------
def test_log5_matches_the_published_formula():
    # A .600 team against a .500 team is a .600 favourite; against itself, .500.
    assert sp.log5(0.6, 0.5) == pytest.approx(0.6)
    assert sp.log5(0.6, 0.6) == pytest.approx(0.5)
    assert sp.log5(0.7, 0.3) == pytest.approx(0.7 * 0.7 / (0.7 * 0.7 + 0.3 * 0.3))
    # Symmetry: swapping the teams must complement the probability.
    assert sp.log5(0.62, 0.44) + sp.log5(0.44, 0.62) == pytest.approx(1.0)


def test_log5_provider_adds_home_advantage():
    provider = sp.Log5Provider()
    data = _data([_standing("LAD", 50, 50, 400, 400), _standing("SEA", 50, 50, 400, 400)])
    [rating] = provider.rate([MATCHUP], data)
    # Evenly matched teams: the only edge left is the home side's.
    assert rating.margin_home == pytest.approx(MLB.home_advantage_margin, abs=1e-6)
    assert rating.p_home > 0.5
    assert rating.p_home == pytest.approx(
        prob_home_from_margin(rating.margin_home, MLB.margin_sigma))


def test_log5_provider_prefers_the_better_record():
    provider = sp.Log5Provider()
    data = _data([_standing("LAD", 65, 35, 500, 400), _standing("SEA", 40, 60, 400, 500)])
    [rating] = provider.rate([MATCHUP], data)
    assert rating.margin_home > 1.0
    assert rating.raw["home_record"] == "65-35" and rating.raw["away_record"] == "40-60"


def test_log5_provider_abstains_early_in_a_season_and_on_missing_teams():
    provider = sp.Log5Provider()
    assert provider.rate([MATCHUP], _data([_standing("LAD", 3, 2, 20, 18),
                                           _standing("SEA", 2, 3, 18, 20)])) == []
    assert provider.rate([MATCHUP], _data([_standing("LAD", 60, 40, 500, 400)])) == []


# --------------------------------------------------------------------------
# Pythagorean
# --------------------------------------------------------------------------
def test_pythagorean_disagrees_with_the_record_when_a_team_has_been_lucky():
    """Two teams with identical records but opposite run differentials must be
    rated differently -- that disagreement with log5 is why this member exists."""
    lucky = _standing("LAD", 55, 45, 400, 450)     # winning despite being outscored
    unlucky = _standing("SEA", 55, 45, 480, 400)   # outscoring opponents, same record

    log5_rating = sp.Log5Provider().rate([MATCHUP], _data([lucky, unlucky]))[0]
    pythag_rating = sp.PythagoreanProvider().rate([MATCHUP], _data([lucky, unlucky]))[0]

    assert log5_rating.margin_home == pytest.approx(MLB.home_advantage_margin, abs=1e-6)
    assert pythag_rating.margin_home < log5_rating.margin_home    # home side flattered
    assert pythag_rating.raw["home_expected_win_pct"] < 0.5


def test_pythagorean_expectation_uses_the_configured_exponent():
    provider = sp.PythagoreanProvider(exponent=2.0)
    s = _standing("LAD", 50, 50, 600.0, 400.0)
    # 600^2 / (600^2 + 400^2) = 0.692
    assert provider._expected_win_pct(s) == pytest.approx(0.6923, abs=1e-4)


def test_pythagorean_abstains_when_a_team_has_not_scored():
    provider = sp.PythagoreanProvider()
    assert provider.rate([MATCHUP], _data([_standing("LAD", 20, 20, 0, 150),
                                           _standing("SEA", 20, 20, 150, 150)])) == []


# --------------------------------------------------------------------------
# Elo
# --------------------------------------------------------------------------
def _game(away, home, away_score, home_score, ts):
    return sp.CompletedGame(away_code=away, home_code=home, away_score=away_score,
                            home_score=home_score, start_ts=ts)


def test_elo_rewards_winning_and_abstains_until_it_has_seen_enough():
    provider = sp.EloProvider()
    # LAD wins four, SEA loses four, both against each other.
    games = [_game("SEA", "LAD", 1, 5, 1000 + i) for i in range(4)]
    [rating] = provider.rate([MATCHUP], _data(completed=games))
    assert rating.raw["home_elo"] > 1500 > rating.raw["away_elo"]
    assert rating.margin_home > MLB.home_advantage_margin

    # One game short of elo_min_games (4) and the member says nothing.
    assert provider.rate([MATCHUP], _data(completed=games[:3])) == []
    assert provider.rate([MATCHUP], _data(completed=[])) == []


def test_elo_is_zero_sum_and_ordered_by_result():
    provider = sp.EloProvider()
    losses = [_game("SEA", "LAD", 9, 1, 1000 + i) for i in range(4)]
    [rating] = provider.rate([MATCHUP], _data(completed=losses))
    assert rating.raw["home_elo"] < 1500 < rating.raw["away_elo"]
    # Elo transfers rating; nothing is created.
    assert (rating.raw["home_elo"] - 1500) == pytest.approx(-(rating.raw["away_elo"] - 1500))


def test_elo_weighs_a_win_over_a_strong_opponent_more_than_one_over_a_weak_one():
    """The property that makes Elo a different signal from a win-loss record."""
    provider = sp.EloProvider()
    # BOS beats everyone; NYY is beaten by everyone. Then LAD beats BOS, and in
    # the second history LAD beats NYY instead.
    base = ([_game("NYY", "BOS", 0, 5, 100 + i) for i in range(6)]
            + [_game("SEA", "LAD", 2, 3, 200 + i) for i in range(4)])
    beat_strong = base + [_game("BOS", "LAD", 1, 4, 900)]
    beat_weak = base + [_game("NYY", "LAD", 1, 4, 900)]

    strong = provider.rate([MATCHUP], _data(completed=beat_strong))[0]
    weak = provider.rate([MATCHUP], _data(completed=beat_weak))[0]
    assert strong.raw["home_elo"] > weak.raw["home_elo"]


def test_elo_treats_a_tie_as_half_a_win():
    provider = sp.EloProvider()
    games = [_game("SEA", "LAD", 3, 3, 1000 + i) for i in range(4)]
    [rating] = provider.rate([MATCHUP], _data(completed=games))
    # The home side was the favourite (home advantage), so drawing four costs it.
    assert rating.raw["home_elo"] < 1500


# --------------------------------------------------------------------------
# Bookmaker member (opt-in)
# --------------------------------------------------------------------------
def test_devig_moneyline_removes_the_book_margin():
    # -126 / +104 (a real DraftKings pair from the live feed): the raw implied
    # probabilities sum to more than 1, and the vig must come out.
    raw_home = 126 / 226
    raw_away = 100 / 204
    assert raw_home + raw_away > 1.0
    p = sp.devig_moneyline(-126, 104)
    assert p == pytest.approx(raw_home / (raw_home + raw_away))
    assert 0.5 < p < 0.58
    # A symmetric market is a coin flip, and a one-sided quote is unusable.
    assert sp.devig_moneyline(-110, -110) == pytest.approx(0.5)
    assert sp.devig_moneyline(-110, None) is None


def test_bookmaker_prefers_the_spread_and_reads_it_home_relative():
    line = ec.BookmakerLine(provider_name="DraftKings", spread_home=-1.5,
                            moneyline_home=-126, moneyline_away=104)
    # spread -1.5 means the home team is favoured by 1.5 runs.
    assert sp.EspnBookmakerProvider._margin(line, MLB) == pytest.approx(1.5)


def test_bookmaker_falls_back_to_the_moneyline_when_no_spread_is_posted():
    line = ec.BookmakerLine(provider_name="DraftKings", spread_home=None,
                            moneyline_home=-126, moneyline_away=104)
    margin = sp.EspnBookmakerProvider._margin(line, MLB)
    assert margin is not None
    assert prob_home_from_margin(margin, MLB.margin_sigma) == pytest.approx(
        sp.devig_moneyline(-126, 104), abs=1e-6)


def test_bookmaker_member_is_not_enabled_by_default():
    """It is another market's price, not an independent model: enabling it
    changes the experiment, so it must never arrive by accident."""
    names = [p.name for p in sp.build_rating_providers(["elo", "log5", "pythagorean"])]
    assert "espn_bookmaker" not in names


def test_build_rating_providers_selects_and_ignores_unknown():
    got = sp.build_rating_providers(["elo", "espn_bookmaker", "not_a_provider"])
    assert [p.name for p in got] == ["elo", "espn_bookmaker"]


def test_build_state_providers():
    assert [p.name for p in sp.build_state_providers(["espn_scoreboard"])] == \
        ["espn_scoreboard"]
    assert sp.build_state_providers(["nope"]) == []


# --------------------------------------------------------------------------
# ESPN feed parsing
# --------------------------------------------------------------------------
SCOREBOARD_EVENT = {
    "id": "401816254",
    "date": "2026-07-25T17:10Z",
    "shortName": "KC @ DET",
    "status": {"clock": 0.0, "displayClock": "0:00", "period": 9,
               "type": {"id": "3", "name": "STATUS_FINAL", "state": "post",
                        "completed": True, "description": "Final"}},
    "competitions": [{
        "id": "401816254",
        "competitors": [
            {"homeAway": "home", "score": "2", "winner": False,
             "team": {"abbreviation": "DET", "displayName": "Detroit Tigers"}},
            {"homeAway": "away", "score": "3", "winner": True,
             "team": {"abbreviation": "KC", "displayName": "Kansas City Royals"}},
        ],
    }],
}


def test_scoreboard_event_parses_into_kalshi_terms():
    g = EspnClient._parse_event(SCOREBOARD_EVENT, "mlb")
    assert (g.away_code, g.home_code, g.pair_code) == ("KC", "DET", "KCDET")
    assert (g.away_score, g.home_score) == (3, 2)
    assert g.completed and g.state == "post" and g.period == 9
    assert g.event_id == "401816254"


def test_scoreboard_event_maps_codes_through_the_league_table():
    event = {**SCOREBOARD_EVENT}
    event["competitions"] = [{"competitors": [
        {"homeAway": "home", "score": "1", "team": {"abbreviation": "CHW"}},
        {"homeAway": "away", "score": "0", "team": {"abbreviation": "ARI"}},
    ]}]
    g = EspnClient._parse_event(event, "mlb")
    assert g.pair_code == "AZCWS"     # ARI->AZ, CHW->CWS


@pytest.mark.parametrize("broken", [
    {"id": "1", "date": "2026-07-25T17:10Z", "competitions": []},
    {"id": "1", "date": "2026-07-25T17:10Z", "competitions": [{"competitors": [
        {"homeAway": "home", "team": {"abbreviation": "DET"}}]}]},        # no away side
    {"id": "1", "competitions": [{"competitors": [                        # no date
        {"homeAway": "home", "team": {"abbreviation": "DET"}},
        {"homeAway": "away", "team": {"abbreviation": "KC"}}]}]},
])
def test_unusable_scoreboard_events_are_dropped_not_half_parsed(broken):
    assert EspnClient._parse_event(broken, "mlb") is None


STANDINGS_TREE = {
    "children": [{
        "name": "American League",
        "children": [{
            "name": "AL East",
            "standings": {"entries": [{
                "team": {"abbreviation": "TB"},
                "stats": [{"name": "wins", "value": 61.0}, {"name": "losses", "value": 43.0},
                          {"name": "pointsFor", "value": 473.0},
                          {"name": "pointsAgainst", "value": 432.0},
                          {"name": "overall", "value": None, "displayValue": "61-43"}],
            }, {
                "team": {"abbreviation": "CHW"},
                "stats": [{"name": "wins", "value": 40.0}, {"name": "losses", "value": 64.0},
                          {"name": "pointsFor", "value": 390.0},
                          {"name": "pointsAgainst", "value": 500.0}],
            }, {
                "team": {"abbreviation": "XXX"},          # incomplete: must be skipped
                "stats": [{"name": "wins", "value": 10.0}],
            }]},
        }],
    }],
}


def test_standings_are_collected_from_nested_children(monkeypatch):
    monkeypatch.setattr(ec, "get_json", lambda url, **kw: STANDINGS_TREE)
    rows = {s.code: s for s in EspnClient().standings("baseball/mlb", "mlb")}
    assert set(rows) == {"TB", "CWS"}          # CHW translated, XXX dropped
    assert rows["TB"].wins == 61 and rows["TB"].losses == 43
    assert rows["TB"].games_played == 104
    assert rows["TB"].points_for == 473.0 and rows["TB"].points_against == 432.0


PICKCENTER = {"pickcenter": [
    {"provider": {"name": "Caesars", "priority": 5}, "spread": -3.5},
    {"provider": {"name": "DraftKings", "priority": 1}, "spread": -1.5,
     "overUnder": 7.0, "details": "TB -126",
     "homeTeamOdds": {"moneyLine": -126}, "awayTeamOdds": {"moneyLine": 104}},
]}


def test_bookmaker_line_takes_the_highest_priority_book(monkeypatch):
    monkeypatch.setattr(ec, "get_json", lambda url, **kw: PICKCENTER)
    line = EspnClient().bookmaker_line("baseball/mlb", "401816254")
    assert line.provider_name == "DraftKings"       # priority 1 beats priority 5
    assert line.spread_home == -1.5
    assert (line.moneyline_home, line.moneyline_away) == (-126, 104)


def test_bookmaker_line_is_none_when_nothing_is_posted(monkeypatch):
    monkeypatch.setattr(ec, "get_json", lambda url, **kw: {"pickcenter": []})
    assert EspnClient().bookmaker_line("baseball/mlb", "401816254") is None


def test_scoreboard_requests_a_single_day_or_a_range(monkeypatch):
    """A wrong `dates` parameter silently returns the wrong games, so the two
    forms the collectors rely on are pinned."""
    seen = []

    def fake(url, **kw):
        seen.append(url)
        return {"events": []}

    monkeypatch.setattr(ec, "get_json", fake)
    from datetime import date
    client = EspnClient()
    client.scoreboard("baseball/mlb", "mlb", date(2026, 7, 26))
    client.scoreboard_range("baseball/mlb", "mlb", date(2026, 7, 20), date(2026, 7, 26))
    assert "dates=20260726" in seen[0]
    assert "dates=20260720-20260726" in seen[1]
