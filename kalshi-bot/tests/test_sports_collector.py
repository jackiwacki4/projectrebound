"""Linking Kalshi sports markets to real games, and the collectors around it.

Linking is the single point of failure in the sports path: if a market is matched
to the wrong game the model will confidently price the wrong teams, and if it is
matched to nothing the model correctly goes silent -- so both failure modes have
to be visible and neither may be papered over.

No network: a stub Kalshi client and a stub ESPN client stand in, both returning
the shapes the real ones were verified to produce.
"""
import logging
from datetime import date

from kalshibot.clients.espn_client import EspnGame, TeamStanding
from kalshibot.clients.sports_providers import Log5Provider
from kalshibot.config import LeagueConfig
from kalshibot.runtime.collectors import SportsCollector, _eastern_date
from kalshibot.util import iso_to_ms

MLB = LeagueConfig(name="mlb", kalshi_series="KXMLBGAME", espn_path="baseball/mlb",
                   margin_sigma=4.4, home_advantage_margin=0.25, regulation_periods=9,
                   elo_min_games=4)

LOG = logging.getLogger("test")


def _game(event_id, pair_start_iso, away, home, *, state="pre", completed=False,
          period=None, away_score=None, home_score=None):
    return EspnGame(event_id=event_id, start_ts=iso_to_ms(pair_start_iso),
                    away_code=away, home_code=home, state=state, completed=completed,
                    period=period, away_score=away_score, home_score=home_score,
                    raw={"id": event_id})


class StubKalshi:
    def __init__(self, markets):
        self._markets = markets

    def list_markets(self, series_ticker=None, status="open", limit=200):
        return [m for m in self._markets if m["ticker"].startswith(f"{series_ticker}-")]


class StubEspn:
    def __init__(self, games=(), standings=()):
        self._games = list(games)
        self._standings = list(standings)
        self.days_requested = []

    def scoreboard(self, espn_path, league, day):
        self.days_requested.append(day)
        return [g for g in self._games if _eastern_date(g.start_ts) == day]

    def standings(self, espn_path, league):
        return list(self._standings)


def _collector(dao, kalshi, espn, **kwargs):
    return SportsCollector(dao, kalshi, [Log5Provider()], [], espn, LOG, **kwargs)


# --------------------------------------------------------------------------
# Match selection
# --------------------------------------------------------------------------
def test_match_uses_start_time_to_split_a_doubleheader():
    """Both games share a pair code and a date; only the time distinguishes them."""
    early = _game("1", "2026-07-28T17:10Z", "SEA", "LAD")
    late = _game("2", "2026-07-28T23:10Z", "SEA", "LAD")
    picked = SportsCollector._match("SEALAD", iso_to_ms("2026-07-28T23:10Z"),
                                    date(2026, 7, 28), [early, late])
    assert picked.event_id == "2"


def test_match_rejects_a_game_more_than_twelve_hours_off():
    """Tomorrow's game between the same two teams must not be accepted for
    today's ticker just because nothing better is on offer."""
    tomorrow = _game("9", "2026-07-29T23:10Z", "SEA", "LAD")
    assert SportsCollector._match("SEALAD", iso_to_ms("2026-07-28T02:10Z"),
                                  date(2026, 7, 27), [tomorrow]) is None


def test_match_falls_back_to_the_date_when_the_ticker_has_no_time():
    """NFL tickers carry no start time, so the date has to carry the match."""
    g = _game("5", "2026-08-15T17:00Z", "DAL", "SEA")
    assert SportsCollector._match("DALSEA", None, date(2026, 8, 15), [g]).event_id == "5"


def test_match_declines_when_a_timeless_ticker_is_ambiguous():
    a = _game("5", "2026-08-15T17:00Z", "DAL", "SEA")
    b = _game("6", "2026-08-15T21:00Z", "DAL", "SEA")
    assert SportsCollector._match("DALSEA", None, date(2026, 8, 15), [a, b]) is None


def test_match_requires_an_exact_pair_code():
    """A near-miss is a team-code mapping bug; smoothing it over would price the
    wrong game."""
    g = _game("7", "2026-07-28T23:10Z", "SEA", "LAA")     # Angels, not Dodgers
    assert SportsCollector._match("SEALAD", iso_to_ms("2026-07-28T23:10Z"),
                                  date(2026, 7, 28), [g]) is None


def test_eastern_date_groups_a_late_game_with_its_own_day():
    """A 10:10pm Eastern game starts after midnight UTC. Grouped by UTC date it
    would land on the wrong day and never match its ticker."""
    assert _eastern_date(iso_to_ms("2026-07-29T02:10:00Z")) == date(2026, 7, 28)


# --------------------------------------------------------------------------
# poll_schedule
# --------------------------------------------------------------------------
MARKETS = [
    {"ticker": "KXMLBGAME-26JUL282210SEALAD-SEA",
     "event_ticker": "KXMLBGAME-26JUL282210SEALAD"},
    {"ticker": "KXMLBGAME-26JUL282210SEALAD-LAD",
     "event_ticker": "KXMLBGAME-26JUL282210SEALAD"},
    {"ticker": "KXMLBTOTAL-26JUL261410ATHMIN-9",           # not a winner market
     "event_ticker": "KXMLBTOTAL-26JUL261410ATHMIN"},
]


def test_poll_schedule_links_both_sides_of_a_game_to_one_row(dao):
    espn = StubEspn([_game("401816999", "2026-07-29T02:10Z", "SEA", "LAD")])
    collector = _collector(dao, StubKalshi(MARKETS), espn)
    assert collector.poll_schedule([MLB]) == 1     # one game, though two markets

    row = dao.conn.execute("SELECT * FROM sports_games").fetchone()
    assert row["game_key"] == "KXMLBGAME-26JUL282210SEALAD"
    assert (row["away_code"], row["home_code"]) == ("SEA", "LAD")
    assert row["source_event_id"] == "401816999"
    assert row["start_ts"] == iso_to_ms("2026-07-29T02:10Z")   # ESPN's time, not the ticker's


def test_poll_schedule_widens_the_window_by_a_day_each_side(dao):
    """Timezone edges: the ticker's Eastern date and the feed's grouping can
    differ by a day, so neighbours are always fetched."""
    espn = StubEspn([])
    _collector(dao, StubKalshi(MARKETS), espn).poll_schedule([MLB])
    assert set(espn.days_requested) == {date(2026, 7, 27), date(2026, 7, 28),
                                        date(2026, 7, 29)}


def test_poll_schedule_writes_nothing_when_no_game_matches(dao):
    """An unlinked market leaves the model with no opinion -- which is correct,
    and must not be masked by a half-filled row."""
    collector = _collector(dao, StubKalshi(MARKETS), StubEspn([]))
    assert collector.poll_schedule([MLB]) == 0
    assert dao.conn.execute("SELECT COUNT(*) AS c FROM sports_games").fetchone()["c"] == 0


def test_poll_schedule_ignores_markets_that_are_not_game_winners(dao):
    """The totals market in the fixture shares the sports family but not the
    ticker shape; it must be skipped, not mis-parsed."""
    espn = StubEspn([_game("401816999", "2026-07-29T02:10Z", "SEA", "LAD")])
    _collector(dao, StubKalshi(MARKETS), espn).poll_schedule([MLB])
    keys = [r["game_key"] for r in dao.conn.execute("SELECT game_key FROM sports_games")]
    assert keys == ["KXMLBGAME-26JUL282210SEALAD"]


def test_poll_schedule_is_idempotent(dao):
    espn = StubEspn([_game("401816999", "2026-07-29T02:10Z", "SEA", "LAD")])
    collector = _collector(dao, StubKalshi(MARKETS), espn)
    collector.poll_schedule([MLB])
    collector.poll_schedule([MLB])
    assert dao.conn.execute("SELECT COUNT(*) AS c FROM sports_games").fetchone()["c"] == 1


# --------------------------------------------------------------------------
# poll_results / poll_ratings / poll_states
# --------------------------------------------------------------------------
def test_poll_results_stores_only_finished_games_and_dedupes(dao):
    finished = _game("100", "2026-07-25T17:10Z", "KC", "DET", state="post",
                     completed=True, period=9, away_score=3, home_score=2)
    scheduled = _game("101", "2026-07-25T20:05Z", "SD", "MIA")
    collector = _collector(dao, StubKalshi([]), StubEspn([finished, scheduled]),
                           results_lookback_days=3)
    collector.poll_results([MLB])
    collector.poll_results([MLB])       # re-poll: the same game must not double up

    rows = dao.conn.execute("SELECT * FROM sports_results").fetchall()
    assert len(rows) == 1
    assert (rows[0]["away_code"], rows[0]["away_score"]) == ("KC", 3)


def test_poll_results_walks_the_full_lookback_only_once(dao):
    """Results are immutable, so re-reading a month of history every six hours
    would be pure waste on a free API."""
    finished = _game("100", date.today().isoformat() + "T17:10Z", "KC", "DET",
                     state="post", completed=True, period=9, away_score=3, home_score=2)
    espn = StubEspn([finished])
    collector = _collector(dao, StubKalshi([]), espn, results_lookback_days=20)

    collector.poll_results([MLB])
    first_pass = len(espn.days_requested)
    espn.days_requested.clear()
    collector.poll_results([MLB])

    assert first_pass == 21                                  # lookback + today
    assert len(espn.days_requested) == collector._RESULTS_CATCHUP_DAYS + 1


def test_poll_ratings_writes_one_row_per_member_per_game(dao):
    espn = StubEspn([_game("401816999", "2026-07-29T02:10Z", "SEA", "LAD")],
                    standings=[TeamStanding("LAD", 65, 35, 500, 400),
                               TeamStanding("SEA", 40, 60, 400, 500)])
    collector = _collector(dao, StubKalshi(MARKETS), espn)
    collector.poll_schedule([MLB])
    assert collector.poll_ratings([MLB]) == 1

    row = dao.conn.execute("SELECT * FROM sports_ratings").fetchone()
    assert row["provider"] == "log5"
    assert row["margin_home"] > 0        # the better team is at home
    assert row["fetched_ts"] >= row["issued_ts"] - 1000


def test_pending_matchups_drops_games_that_have_finished(dao):
    espn = StubEspn([_game("401816999", "2026-07-29T02:10Z", "SEA", "LAD")])
    collector = _collector(dao, StubKalshi(MARKETS), espn)
    collector.poll_schedule([MLB])
    assert len(collector._pending_matchups(MLB)) == 1

    dao.insert_sports_game_state(game_key="KXMLBGAME-26JUL282210SEALAD", obs_ts=1,
                                 state="post", completed=True, period=9,
                                 home_score=5, away_score=2, raw={})
    assert collector._pending_matchups(MLB) == []


def test_poll_states_records_only_games_that_have_a_market(dao):
    """The scoreboard carries every game in the league; storing states for games
    nobody is trading would be noise.

    Live state is only collected for today and yesterday, so this fixture is
    dated relative to the real clock rather than pinned like the others.
    """
    today = date.today()
    stamp = today.strftime("%y%b%d").upper()          # e.g. 26JUL26
    game_key = f"KXMLBGAME-{stamp}1910SEALAD"
    markets = [{"ticker": f"{game_key}-SEA", "event_ticker": game_key},
               {"ticker": f"{game_key}-LAD", "event_ticker": game_key}]
    start_iso = f"{today.isoformat()}T23:10:00Z"      # 7:10pm Eastern the same day
    ours = _game("401816999", start_iso, "SEA", "LAD", state="in",
                 period=4, away_score=1, home_score=2)
    theirs = _game("999", start_iso, "NYY", "PHI", state="in",
                   period=4, away_score=0, home_score=0)
    espn = StubEspn([ours, theirs])
    from kalshibot.clients.sports_providers import EspnScoreboardStateProvider
    collector = SportsCollector(dao, StubKalshi(markets), [],
                                [EspnScoreboardStateProvider(espn)], espn, LOG)
    assert collector.poll_schedule([MLB]) == 1
    collector.poll_states([MLB])

    rows = dao.conn.execute("SELECT * FROM sports_game_states").fetchall()
    assert {r["game_key"] for r in rows} == {game_key}
    assert rows[0]["home_score"] == 2 and rows[0]["period"] == 4


def test_collectors_survive_a_failing_feed(dao):
    """One dead source must not stop the sweep -- the weather collectors set this
    precedent and the report's diagnosis depends on it."""
    class Broken(StubEspn):
        def scoreboard(self, espn_path, league, day):
            raise RuntimeError("ESPN 503")

        def standings(self, espn_path, league):
            raise RuntimeError("ESPN 503")

    collector = _collector(dao, StubKalshi(MARKETS), Broken(), results_lookback_days=1)
    assert collector.poll_schedule([MLB]) == 0
    assert collector.poll_results([MLB]) == 0
    assert collector.poll_ratings([MLB]) == 0
