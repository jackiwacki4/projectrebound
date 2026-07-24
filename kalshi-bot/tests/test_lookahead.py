"""No-lookahead structural enforcement (spec section 6).

The model is only ever handed an AsOfView. These tests assert that view
physically cannot surface any record whose "known at" timestamp is after the
as-of instant -- so lookahead bias is impossible, not merely discouraged.
"""


def _insert_forecast(dao, station, issued_ts, fetched_ts, target_date, high_f):
    # Insert directly with an explicit fetched_ts to simulate history.
    dao.conn.execute(
        "INSERT INTO forecasts(station, city, issued_ts, fetched_ts, target_date, "
        "forecast_high_f, raw) VALUES(?,?,?,?,?,?,?)",
        (station, None, issued_ts, fetched_ts, target_date, high_f, "{}"),
    )


def test_as_of_hides_forecasts_fetched_after_the_cutoff(dao):
    T = 1_000_000
    _insert_forecast(dao, "KMDW", issued_ts=T - 500, fetched_ts=T - 100,
                     target_date="2026-07-24", high_f=88.0)          # known before T
    _insert_forecast(dao, "KMDW", issued_ts=T + 500, fetched_ts=T + 100,
                     target_date="2026-07-24", high_f=95.0)          # known after T

    view = dao.as_of(T)
    fc = view.latest_forecast("KMDW", "2026-07-24")
    assert fc is not None
    assert fc.forecast_high_f == 88.0          # the future forecast is invisible
    assert view.count_visible_forecasts("KMDW") == 1


def test_as_of_hides_book_snapshots_captured_after_the_cutoff(dao):
    T = 2_000_000
    dao.insert_book_snapshot("MKT", captured_ts=T - 10, yes_levels=[[45, 100]],
                             no_levels=[[53, 100]])
    dao.insert_book_snapshot("MKT", captured_ts=T + 10, yes_levels=[[60, 100]],
                             no_levels=[[38, 100]])

    view = dao.as_of(T)
    book = view.latest_book("MKT")
    assert book is not None
    assert book.captured_ts == T - 10          # not the later snapshot
    assert book.yes_levels == [[45, 100]]


def test_as_of_returns_none_when_nothing_was_known_yet(dao):
    dao.insert_book_snapshot("MKT", captured_ts=5000, yes_levels=[[45, 1]],
                             no_levels=[[53, 1]])
    # As of an instant before any data existed, the view sees nothing.
    assert dao.as_of(4000).latest_book("MKT") is None


def test_append_only_book_history_is_preserved(dao):
    for i in range(5):
        dao.insert_book_snapshot("MKT", captured_ts=1000 + i, yes_levels=[[40 + i, 1]],
                                 no_levels=[[59 - i, 1]])
    rows = dao.conn.execute("SELECT COUNT(*) AS c FROM book_snapshots").fetchone()
    assert rows["c"] == 5     # every snapshot retained, nothing overwritten
