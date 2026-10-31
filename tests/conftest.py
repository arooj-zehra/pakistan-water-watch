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
