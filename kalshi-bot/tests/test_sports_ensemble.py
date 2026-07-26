"""Ensemble sports model: member combination, disagreement-as-uncertainty, the
live-score clamp, and no-lookahead across the member dimension.

The sports counterpart of test_forecast_ensemble.py, and deliberately parallel to
it: the same properties matter, because it is the same model shape applied to a
different driver.
"""
import math

import pytest

from kalshibot.config import LeagueConfig
from kalshibot.models.base import MarketContext
from kalshibot.models.sports import (
    SportsEnsembleModel,
    SportsMarket,
    fraction_remaining,
    margin_from_prob,
    prob_home_from_margin,
)
from kalshibot.util import now_ms

MLB = LeagueConfig(name="mlb", kalshi_series="KXMLBGAME", espn_path="baseball/mlb",
                   margin_sigma=4.4, home_advantage_margin=0.25, regulation_periods=9)

GAME_KEY = "KXMLBGAME-26JUL282210SEALAD"

# insert_sports_rating / insert_sports_game_state stamp their "known at" columns
# at insertion time, so deciding at exactly `now` races the write. Decide
# slightly after seeding, as production always does.
_DECIDE_AFTER_MS = 1000


def _market(yes_team="LAD", start_ts=None):
    return SportsMarket(ticker=f"{GAME_KEY}-{yes_team}", series="KXMLBGAME", league="mlb",
                        game_key=GAME_KEY, yes_team=yes_team, pair_code="SEALAD",
                        target_date="2026-07-28", start_ts=start_ts)


def _seed_game(dao, start_ts=None):
    dao.upsert_sports_game(game_key=GAME_KEY, league="mlb", series="KXMLBGAME",
                           away_code="SEA", home_code="LAD", start_ts=start_ts,
                           source_event_id="401816999")


def _seed_rating(dao, provider, margin_home, *, fetched_ts=None):
    """Insert a member. `fetched_ts` writes history directly, as the collectors
    cannot (they always stamp 'now')."""
    if fetched_ts is None:
        dao.insert_sports_rating(provider=provider, game_key=GAME_KEY, league="mlb",
                                 issued_ts=now_ms(), margin_home=margin_home,
                                 p_home=0.5, raw={})
        return
    dao.conn.execute(
        "INSERT INTO sports_ratings(provider, game_key, league, issued_ts, fetched_ts, "
        "margin_home, p_home, raw) VALUES(?,?,?,?,?,?,?,?)",
        (provider, GAME_KEY, "mlb", fetched_ts, fetched_ts, margin_home, 0.5, "{}"),
    )


def _seed_state(dao, *, state="in", completed=False, period=5, home=0, away=0):
    dao.insert_sports_game_state(game_key=GAME_KEY, obs_ts=now_ms(), state=state,
                                 completed=completed, period=period,
                                 home_score=home, away_score=away, raw={})


def _model(**kwargs):
    return SportsEnsembleModel([MLB], **kwargs)


def _predict(dao, market, at):
    return _model().predict(market, MarketContext(view=dao.as_of(at), decision_ts=at))


# --------------------------------------------------------------------------
# The probability <-> margin link
# --------------------------------------------------------------------------
def test_even_matchup_is_a_coin_flip_in_both_directions():
    """A zero expected margin must be 50/50, and 50/50 must invert to zero.

    The naive inverse (sigma * ppf(p)) breaks this: it ignores the tie-mass
    renormalisation and hands an evenly-matched game a half-run home edge.
    """
    assert prob_home_from_margin(0.0, 4.4) == pytest.approx(0.5)
    assert margin_from_prob(0.5, 4.4) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("p", [0.05, 0.25, 0.4, 0.5, 0.62, 0.8, 0.95])
def test_margin_and_probability_round_trip(p):
    assert prob_home_from_margin(margin_from_prob(p, 4.4), 4.4) == pytest.approx(p, abs=1e-6)


def test_the_two_sides_of_a_game_always_sum_to_one():
    """Kalshi lists both teams as separate markets; the model must price them
    consistently or it will happily 'find edge' on both at once."""
    for mu in (-6.0, -1.0, 0.0, 0.5, 3.3):
        p_home = prob_home_from_margin(mu, 4.4)
        p_away = prob_home_from_margin(-mu, 4.4)
        assert p_home + p_away == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------
# Ensemble combination
# --------------------------------------------------------------------------
def test_ensemble_combines_members_and_widens_sigma_on_disagreement(dao):
    _seed_game(dao)
    for provider, margin in [("elo", 0.4), ("log5", 0.8), ("pythagorean", 1.2)]:
        _seed_rating(dao, provider, margin)

    at = now_ms() + _DECIDE_AFTER_MS
    pred = _predict(dao, _market("LAD"), at)
    assert pred is not None
    assert pred.inputs["ensemble_mean_margin"] == 0.8            # mean of .4/.8/1.2
    assert pred.inputs["ensemble_spread_margin"] == 0.4          # stdev of .4/.8/1.2
    # Pre-game, f = 1, so effective sigma = sqrt(4.4^2 + 0.4^2) = 4.418.
    assert pred.uncertainty == pytest.approx(math.sqrt(4.4 ** 2 + 0.4 ** 2), abs=1e-3)
    assert set(pred.inputs["ensemble_members"]) == {"elo", "log5", "pythagorean"}
    # A 0.8-run edge to the home side is a modest favourite, not a lock.
    assert 0.53 < pred.probability < 0.60


def test_disagreement_pushes_the_probability_toward_a_coin_flip(dao):
    """Same mean margin, more disagreement -> less confidence. This is the whole
    point of ensembling rather than picking one method."""
    _seed_game(dao)
    for provider, margin in [("elo", 3.0), ("log5", 3.0)]:
        _seed_rating(dao, provider, margin)
    at = now_ms() + _DECIDE_AFTER_MS
    agreed = _predict(dao, _market("LAD"), at).probability

    dao.conn.execute("DELETE FROM sports_ratings")
    for provider, margin in [("elo", 0.0), ("log5", 6.0)]:
        _seed_rating(dao, provider, margin)
    at = now_ms() + _DECIDE_AFTER_MS
    divided = _predict(dao, _market("LAD"), at).probability

    assert agreed > divided > 0.5


def test_orientation_follows_the_yes_team(dao):
    """The same game priced from both sides must be complementary."""
    _seed_game(dao)
    _seed_rating(dao, "elo", 1.5)          # home (LAD) favoured
    at = now_ms() + _DECIDE_AFTER_MS
    home_side = _predict(dao, _market("LAD"), at)
    away_side = _predict(dao, _market("SEA"), at)
    assert home_side.probability > 0.5 > away_side.probability
    assert home_side.probability + away_side.probability == pytest.approx(1.0, abs=1e-9)
    assert home_side.inputs["yes_is_home"] is True
    assert away_side.inputs["yes_is_home"] is False


def test_no_members_means_no_opinion(dao):
    _seed_game(dao)
    assert _predict(dao, _market("LAD"), now_ms()) is None


def test_unlinked_game_means_no_opinion(dao):
    """Without a linked schedule row the model does not know who is at home,
    and guessing would invert half of its probabilities."""
    _seed_rating(dao, "elo", 1.5)
    assert _predict(dao, _market("LAD"), now_ms() + _DECIDE_AFTER_MS) is None


def test_ticker_team_absent_from_the_linked_game_is_declined(dao):
    _seed_game(dao)
    _seed_rating(dao, "elo", 1.5)
    at = now_ms() + _DECIDE_AFTER_MS
    assert _predict(dao, _market("BOS"), at) is None


# --------------------------------------------------------------------------
# The live-score clamp (the sports analog of the METAR clamp)
# --------------------------------------------------------------------------
def test_lead_shifts_the_distribution_and_time_shrinks_it(dao):
    """A 3-run lead in the 8th is worth far more than the same lead in the 2nd."""
    start = now_ms() - 3_600_000
    _seed_game(dao, start_ts=start)
    _seed_rating(dao, "elo", 0.0)          # evenly matched, so the lead is the story

    _seed_state(dao, period=2, home=3, away=0)
    at = now_ms() + _DECIDE_AFTER_MS
    early = _predict(dao, _market("LAD"), at)

    dao.conn.execute("DELETE FROM sports_game_states")
    _seed_state(dao, period=8, home=3, away=0)
    at = now_ms() + _DECIDE_AFTER_MS
    late = _predict(dao, _market("LAD"), at)

    assert 0.5 < early.probability < late.probability < 1.0
    assert late.uncertainty < early.uncertainty        # less game left, less doubt
    assert early.inputs["live_home_lead"] == 3
    assert late.inputs["fraction_remaining"] < early.inputs["fraction_remaining"]


def test_trailing_late_is_priced_below_a_coin_flip(dao):
    start = now_ms() - 3_600_000
    _seed_game(dao, start_ts=start)
    _seed_rating(dao, "elo", 2.0)          # home favoured pre-game...
    _seed_state(dao, period=9, home=0, away=4)   # ...but losing 4-0 in the 9th
    at = now_ms() + _DECIDE_AFTER_MS
    pred = _predict(dao, _market("LAD"), at)
    assert pred.probability < 0.10


def test_final_score_forces_certainty(dao):
    start = now_ms() - 10_800_000
    _seed_game(dao, start_ts=start)
    _seed_rating(dao, "elo", -3.0)         # the model expected the away side
    _seed_state(dao, state="post", completed=True, period=9, home=5, away=2)
    at = now_ms() + _DECIDE_AFTER_MS
    assert _predict(dao, _market("LAD"), at).probability == 1.0
    assert _predict(dao, _market("SEA"), at).probability == 0.0


def test_completed_tie_is_declined_rather_than_priced(dao):
    """An NFL tie resolves both sides to No. There is no honest probability to
    quote, so the model declines instead of inventing one."""
    start = now_ms() - 10_800_000
    _seed_game(dao, start_ts=start)
    _seed_rating(dao, "elo", 0.5)
    _seed_state(dao, state="post", completed=True, period=4, home=17, away=17)
    at = now_ms() + _DECIDE_AFTER_MS
    assert _predict(dao, _market("LAD"), at) is None


def test_started_game_with_a_stale_score_feed_gets_no_opinion(dao):
    """The dangerous case: the game is underway, so the pre-game probability is
    wrong, but the feed that would tell us has died."""
    start = now_ms() - 3_600_000
    _seed_game(dao, start_ts=start)
    _seed_rating(dao, "elo", 1.0)
    _seed_state(dao, period=3, home=0, away=5)
    at = now_ms() + _DECIDE_AFTER_MS

    fresh = SportsEnsembleModel([MLB], max_state_age_seconds=900)
    stale = SportsEnsembleModel([MLB], max_state_age_seconds=0)
    ctx = MarketContext(view=dao.as_of(at), decision_ts=at)
    assert fresh.predict(_market("LAD"), ctx) is not None
    assert stale.predict(_market("LAD"), ctx) is None


def test_started_game_with_no_state_at_all_gets_no_opinion(dao):
    _seed_game(dao, start_ts=now_ms() - 3_600_000)
    _seed_rating(dao, "elo", 1.0)
    assert _predict(dao, _market("LAD"), now_ms() + _DECIDE_AFTER_MS) is None


def test_pre_game_needs_no_state(dao):
    _seed_game(dao, start_ts=now_ms() + 86_400_000)
    _seed_rating(dao, "elo", 1.0)
    assert _predict(dao, _market("LAD"), now_ms() + _DECIDE_AFTER_MS) is not None


@pytest.mark.parametrize("period,expected", [(1, 1.0), (5, 5 / 9), (9, 1 / 9)])
def test_fraction_remaining_counts_completed_periods(period, expected):
    from kalshibot.storage.dao import SportsStateRow
    state = SportsStateRow(game_key=GAME_KEY, obs_ts=0, captured_ts=0, state="in",
                           completed=False, period=period, home_score=0, away_score=0,
                           raw={})
    assert fraction_remaining(state, 9) == pytest.approx(expected)


def test_extra_innings_keeps_a_sliver_of_uncertainty():
    """Nothing is decided in a tied 11th inning; a zero fraction would claim it is."""
    from kalshibot.storage.dao import SportsStateRow
    state = SportsStateRow(game_key=GAME_KEY, obs_ts=0, captured_ts=0, state="in",
                           completed=False, period=11, home_score=4, away_score=4,
                           raw={})
    f = fraction_remaining(state, 9)
    assert 0 < f <= 0.05
    assert prob_home_from_margin(0.0, 4.4 * math.sqrt(f)) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# Structural guarantees
# --------------------------------------------------------------------------
def test_no_lookahead_across_members(dao):
    # Seed the game FIRST, then pick the decision instant after it: like
    # observations, upsert_sports_game stamps first_seen_ts at insertion time, so
    # an as-of of exactly `now` races the write and the game row can land a
    # millisecond in the "future" -- correctly hidden, but flaky here.
    _seed_game(dao)
    T = now_ms() + _DECIDE_AFTER_MS
    _seed_rating(dao, "elo", 0.5, fetched_ts=T - 1000)      # known
    _seed_rating(dao, "log5", 9.0, fetched_ts=T + 1000)     # not yet known
    pred = _predict(dao, _market("LAD"), T)
    assert pred is not None
    assert list(pred.inputs["ensemble_members"]) == ["elo"]


def test_latest_per_member_wins(dao):
    _seed_game(dao)
    T = now_ms() + _DECIDE_AFTER_MS
    _seed_rating(dao, "elo", 0.1, fetched_ts=T - 5000)
    _seed_rating(dao, "elo", 2.5, fetched_ts=T - 1000)
    members = dao.as_of(T).latest_sports_ratings_by_provider(GAME_KEY)
    assert members["elo"].margin_home == 2.5


def test_min_sigma_floor_caps_confidence_when_members_falsely_agree(dao):
    """The members share a data source, so unanimity is not evidence. The floor
    is what stops that unanimity from being priced as certainty."""
    _seed_game(dao)
    for provider in ("elo", "log5", "pythagorean"):
        _seed_rating(dao, provider, 4.0)        # identical: zero spread
    at = now_ms() + _DECIDE_AFTER_MS
    ctx = MarketContext(view=dao.as_of(at), decision_ts=at)
    loose = SportsEnsembleModel([MLB], min_sigma_floor=0.0).predict(_market("LAD"), ctx)
    floored = SportsEnsembleModel([MLB], min_sigma_floor=8.0).predict(_market("LAD"), ctx)
    assert floored.uncertainty > loose.uncertainty
    assert 0.5 < floored.probability < loose.probability


def test_unknown_league_is_declined(dao):
    _seed_game(dao)
    _seed_rating(dao, "elo", 1.0)
    at = now_ms() + _DECIDE_AFTER_MS
    other = SportsMarket(ticker="X", series="KXNFLGAME", league="nfl", game_key=GAME_KEY,
                         yes_team="LAD", pair_code="SEALAD", target_date="2026-07-28",
                         start_ts=None)
    assert _model().predict(other, MarketContext(view=dao.as_of(at), decision_ts=at)) is None


def test_every_input_needed_to_audit_the_call_is_persisted(dao):
    """A wrong call has to be readable after the fact, not re-derived."""
    _seed_game(dao, start_ts=now_ms() - 60_000)
    _seed_rating(dao, "elo", 1.0)
    _seed_state(dao, period=4, home=2, away=1)
    at = now_ms() + _DECIDE_AFTER_MS
    inputs = _predict(dao, _market("LAD"), at).inputs
    for key in ("ensemble_members", "ensemble_mean_margin", "ensemble_spread_margin",
                "base_margin_sigma", "effective_sigma", "fraction_remaining",
                "live_home_lead", "game_state", "game_period", "state_age_seconds",
                "p_home", "yes_team", "home_code", "away_code", "yes_is_home",
                "league", "game_key", "as_of_ms"):
        assert key in inputs, f"model inputs are missing {key}"
