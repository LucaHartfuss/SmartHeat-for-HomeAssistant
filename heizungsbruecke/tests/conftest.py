import pytest


class FakeClock:
    """Monotone Test-Uhr fuer RegulationWorker: steht still, bis ein Test sie vorstellt."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()
