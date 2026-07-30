"""Provider wiring: model-name mapping and error-message quality.

These are offline tests. The Open-Meteo model identifiers themselves were
verified against the live API (see the comment on _OPEN_METEO_MODELS); the
point here is that the mapping is wired correctly and that failures explain
themselves instead of surfacing a bare status code.
"""
import io
import urllib.error

import pytest

from kalshibot.clients import forecast_providers as fp
from kalshibot.clients import http_json


def test_provider_names_map_to_verified_model_ids():
    m = fp._OPEN_METEO_MODELS
    assert m["open_meteo_hrrr"] == "gfs_hrrr"
    assert m["open_meteo_gfs"] == "gfs_global"
    assert m["open_meteo_ecmwf"] == "ecmwf_ifs025"
    assert m["open_meteo_best"] is None       # auto blend, no models= param


def test_gfs_seamless_not_used_it_would_duplicate_hrrr():
    # gfs_seamless blends HRRR into the near term; using it alongside the HRRR
    # member would fake agreement and shrink the ensemble spread artificially.
    assert "gfs_seamless" not in fp._OPEN_METEO_MODELS.values()


def test_build_forecast_providers_selects_and_ignores_unknown():
    got = fp.build_forecast_providers(["nws", "open_meteo_hrrr", "not_a_provider"])
    assert [p.name for p in got] == ["nws", "open_meteo_hrrr"]


def test_build_observation_providers():
    assert [p.name for p in fp.build_observation_providers(["metar"])] == ["metar"]


def test_http_errors_include_the_server_explanation(monkeypatch):
    """A bare 'HTTP Error 400: Bad Request' hides the reason; the body must survive.

    The fetch itself lives in clients/http_json.py (shared with the sports feeds),
    which is what `fp._get_json` resolves to -- patched here at its own module so
    the dependency is explicit.
    """
    body = b'{"error":true,"reason":"Cannot initialize MultiDomains from invalid String value bogus."}'

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            "https://api.open-meteo.com/v1/forecast", 400, "Bad Request", {},
            io.BytesIO(body))

    monkeypatch.setattr(http_json.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc:
        fp._get_json("https://api.open-meteo.com/v1/forecast?models=bogus")

    assert "invalid String value bogus" in str(exc.value)
