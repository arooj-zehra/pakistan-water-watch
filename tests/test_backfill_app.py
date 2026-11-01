"""backfill_app is now a thin wrapper; the real tests are in test_backfill_news.py."""
from pipeline import backfill_app


def test_shim_delegates_to_backfill_news(monkeypatch):
    seen = {}
    monkeypatch.setattr(backfill_app, "_news_main", lambda argv: seen.setdefault("argv", argv) and 0)
    backfill_app.main()
    assert seen["argv"] == ["--outlets", "app"]
