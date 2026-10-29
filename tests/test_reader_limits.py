import pytest
from literature.reader_limits import FoldRequestGate
from literature.providers import ProviderError


def test_fold_gate_shares_two_slots_per_service_and_releases():
    gate = FoldRequestGate()
    gate.acquire('svc')
    gate.acquire('svc')
    with pytest.raises(ProviderError) as raised:
        gate.acquire('svc', timeout=0.01)
    assert raised.value.possible_charge is False
    gate.acquire('other-service')
    gate.release('svc')
    gate.acquire('svc', timeout=0.01)
    gate.release('svc')
    gate.release('svc')
    gate.release('other-service')


def test_rate_limit_reduces_concurrency_until_cooldown(monkeypatch):
    gate = FoldRequestGate()
    now = [100.0]
    monkeypatch.setattr('literature.reader_limits.time.monotonic', lambda: now[0])
    gate.acquire('svc')
    gate.release('svc', 429)
    gate.acquire('svc')
    with pytest.raises(ProviderError):
        gate.acquire('svc', timeout=0)
    gate.release('svc')
    now[0] = 161.0
    gate.acquire('svc')
    gate.acquire('svc')
    gate.release('svc')
    gate.release('svc')
