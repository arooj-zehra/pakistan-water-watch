import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def engine():
    from pipeline import db

    eng = db.get_engine("sqlite:///:memory:")
    db.init_db(eng)
    return eng


@pytest.fixture(autouse=True)
def _no_real_review_decisions(tmp_path, monkeypatch):
    """Tests never read or write the repo's data/review/decisions.csv."""
    from pipeline import anomaly

    monkeypatch.setattr(anomaly, "DECISIONS_CSV", tmp_path / "decisions.csv")
