"""Testoffensive T2 (Spec §12 Nr. 20): hypothesis steht als Dev-Abhaengigkeit bereit; Einstellungen bis T6 am Test."""
from hypothesis import given, settings
from hypothesis import strategies as st


@settings(max_examples=50, derandomize=True, database=None)
@given(st.integers())
def test_hypothesis_laeuft_mit_festem_seed(zahl):
    assert isinstance(zahl, int)
