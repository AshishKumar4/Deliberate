"""Containment failure must prevent even a newly created verifier from acting."""
import asyncio
from types import SimpleNamespace

import pytest

from scripts import native_harness as guard


@pytest.mark.asyncio
async def test_failed_containment_blocks_a_fresh_verifier_environment(monkeypatch, tmp_path):
    scored = tmp_path / 'reward.txt'

    class Environment:
        async def _run_docker_compose_command(self, command):
            if self is agent:
                raise asyncio.CancelledError()
            scored.write_text('1')

    agent = Environment()
    agent._native_guard_state = {'prefix': ['docker', 'compose'], 'proc_env': {}}
    monkeypatch.setattr(guard, '_PROCESS_BREACH', {})
    monkeypatch.setattr(guard, '_run_reaper', lambda *args: {'ok': False, 'error': 'cleanup unavailable'})
    monkeypatch.setattr(guard, '_reap_host_client', lambda scope: {})
    monkeypatch.delenv('NATIVE_HARNESS_EVENT_LOG', raising=False)
    monkeypatch.delenv('NATIVE_HARNESS_BREACH_LOG', raising=False)
    guard._patch_compose_runner(SimpleNamespace(cls=Environment, main_service='main', package='fixture'))

    with pytest.raises(asyncio.CancelledError):
        await agent._run_docker_compose_command(['exec', 'main', 'agent'])
    verifier = Environment()
    with pytest.raises(guard.ExecContainmentBreachError):
        await verifier._run_docker_compose_command(['exec', 'main', 'verify'])
    assert not scored.exists()


def test_breach_diagnostics_retain_all_survivor_pids():
    contained, survivors = guard._parse_result('GUARD_RESULT contained=0 survivors= 123 456 \n')
    assert contained is False
    assert survivors.split() == ['123', '456']
