"""Ensemble weather model: multi-provider combination, disagreement-as-uncertainty,
observation clamp, and no-lookahead across the provider dimension."""
import json
from datetime import date

from kalshibot.models.base import Market, MarketContext
from kalshibot.models.weather import WeatherNormalModel
from kalshibot.util import now_ms


def _insert_forecast(dao, provider, station, target_date, high_f, fetched_ts, issued_ts=None):
    dao.conn.execute(
        "INSERT INTO forecasts(provider, station, city, issued_ts, fetched_ts, "
        "target_date, forecast_high_f, raw) VALUES(?,?,?,?,?,?,?,?)",
        (provider, station, None, issued_ts or fetched_ts, fetched_ts, target_date,
         high_f, "{}"),
    )


def _market(target_date, kind="above", lower=88.0, upper=None):
    return Market(ticker="M", series="S", city="Chicago", threshold_kind=kind,
                  lower_f=lower, upper_f=upper, target_date=target_date,
                  nws_station="KMDW", lat=41.786, lon=-87.752)


def test_ensemble_combines_providers_and_widens_sigma_on_disagreement(dao):
    now = now_ms()
    td = "2026-07-24"
    for prov, high in [("nws", 88.0), ("open_meteo_hrrr", 90.0), ("open_meteo_gfs", 92.0)]:
        _insert_forecast(dao, prov, "KMDW", td, high, fetched_ts=now - 1000)

    model = WeatherNormalModel(sigma_f=3.0)
    pred = model.predict(_market(td), MarketContext(view=dao.as_of(now), decision_ts=now))
    assert pred is not None
    assert pred.inputs["ensemble_mean_f"] == 90.0            # mean of 88/90/92
    assert pred.inputs["ensemble_spread_f"] == 2.0           # stdev of 88/90/92
    # effective sigma grows with disagreement: sqrt(3^2 + 2^2) = sqrt(13) ~ 3.61
    assert 3.6 < pred.uncertainty < 3.62
    assert set(pred.inputs["ensemble_members"]) == {"nws", "open_meteo_hrrr", "open_meteo_gfs"}
    # floor_strike 88 means "89 or above", so the boundary is 88.5, not 88:
    # P(X > 88.5 | mean 90, sigma 3.606) ~ 0.661.
    assert 0.65 < pred.probability < 0.67


# insert_observation stamps captured_ts at insertion time, so an as_of of
# exactly `now` races the write -- the row may land a millisecond in the
# "future" and be correctly hidden by the no-lookahead filter, making these
# tests flaky. Decide slightly after seeding, as production always does.
_DECIDE_AFTER_MS = 1000


def test_observation_clamp_forces_certainty_when_already_reached(dao):
    now = now_ms()
    td = date.today().isoformat()
    _insert_forecast(dao, "nws", "KMDW", td, 90.0, fetched_ts=now - 1000)
    # Already observed 89F (31.667C) today -> "above 88" needs 89, so settled true.
    dao.insert_observation("KMDW", obs_ts=now - 500, temp_c=(89 - 32) * 5 / 9, raw={})

    at = now + _DECIDE_AFTER_MS
    model = WeatherNormalModel(sigma_f=3.0)
    pred = model.predict(_market(td, "above", 88.0),
                         MarketContext(view=dao.as_of(at), decision_ts=at))
    assert pred.probability == 1.0
    assert pred.inputs["observed_max_f_so_far"] == 89.0


def test_observation_clamp_forces_zero_for_below_market_once_exceeded(dao):
    now = now_ms()
    td = date.today().isoformat()
    _insert_forecast(dao, "nws", "KMDW", td, 90.0, fetched_ts=now - 1000)
    dao.insert_observation("KMDW", obs_ts=now - 500, temp_c=(89 - 32) * 5 / 9, raw={})

    at = now + _DECIDE_AFTER_MS
    model = WeatherNormalModel(sigma_f=3.0)
    pred = model.predict(_market(td, "below", lower=None, upper=88.0),
                         MarketContext(view=dao.as_of(at), decision_ts=at))
    assert pred.probability == 0.0


def test_no_lookahead_across_providers(dao):
    T = now_ms()
    td = "2026-07-24"
    _insert_forecast(dao, "nws", "KMDW", td, 88.0, fetched_ts=T - 1000)         # known
    _insert_forecast(dao, "open_meteo_gfs", "KMDW", td, 99.0, fetched_ts=T + 1000)  # future

    model = WeatherNormalModel(sigma_f=3.0)
    pred = model.predict(_market(td), MarketContext(view=dao.as_of(T), decision_ts=T))
    assert list(pred.inputs["ensemble_members"]) == ["nws"]   # future member invisible


def test_latest_per_provider_wins(dao):
    now = now_ms()
    td = "2026-07-24"
    _insert_forecast(dao, "nws", "KMDW", td, 80.0, fetched_ts=now - 5000, issued_ts=now - 5000)
    _insert_forecast(dao, "nws", "KMDW", td, 91.0, fetched_ts=now - 1000, issued_ts=now - 1000)
    members = dao.as_of(now).latest_forecasts_by_provider("KMDW", td)
    assert members["nws"].forecast_high_f == 91.0             # most recent nws forecast
