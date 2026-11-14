import random

import pytest

from synth.invoices import build_clean_invoices
from synth.master import build_master
from synth.model import Invoice, MasterData
from tests.helpers import SEED


@pytest.fixture(scope="session")
def master() -> MasterData:
    return build_master(random.Random(SEED), SEED)


@pytest.fixture(scope="session")
def clean(master: MasterData) -> list[Invoice]:
    return build_clean_invoices(master, random.Random(SEED))
