"""Capacity acceptance must actually exercise 20 concurrent new claims."""
import threading

from scripts import load_test_100k as load


def test_new_claim_latency_scenario_starts_twenty_requests_concurrently(monkeypatch, tmp_path):
    gate = threading.Barrier(20)
    mutex = threading.Lock()
    active = 0
    peak = 0

    def claim(client, filters=None):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
        try:
            # Serial requests or fewer than 20 workers cannot satisfy the gate.
            gate.wait(timeout=5)
            return {'http_status': 200, 'ms': 10.0, 'assigned': True,
                    'resumed': False, 'task_id': 'task-' + client,
                    'pool_reason': None, 'error': None}
        finally:
            with mutex:
                active -= 1

    monkeypatch.setattr(load, 'login_annotator', lambda base, username: username)
    monkeypatch.setattr(load, 'timed_claim', claim)
    result = load.run_new_claim_scenario(
        'http://synthetic.invalid', [f'user-{index}' for index in range(20)],
        {}, tmp_path / 'samples.jsonl', 'claim_new_normal',
    )
    assert result['ok'] and result['assigned_task_ids'] == 20
    assert peak == 20, 'The latency acceptance scenario must contain 20 concurrent new claims'
