"""A harness isolation failure must never contribute a capability score."""
import json

from scripts import benchmark


def test_containment_breach_excludes_reward_without_discarding_clean_trial(tmp_path):
    condition = 'ctl/fixture'
    manifest = {
        'suite': 'terminal-bench-2',
        'adjudication': {'infrastructure_excluded': ['ExecContainmentBreachError']},
        'contrasts': [{'id': 'rp_vs_ctl', 'treatment_mode': 'rp', 'reference_mode': 'ctl'}],
        'conditions': [condition],
        'tasks': [{'id': task} for task in ('breached', 'clean')],
        'plan': [{'condition': condition} for _ in range(2)],
        'counts': {'planned_trials': 2},
        'primary_metric': 'official binary reward',
    }
    frozen = tmp_path / 'frozen.json'
    frozen.write_text(json.dumps(manifest))
    execution = {
        'jobs': [
            {'job_name': 'breached-job', 'exec_guard': {'breaches': [{'contained': False}]}},
            {'job_name': 'clean-job', 'exec_guard': {'breaches': []}},
        ],
        'trials': [
            {'trial_key': f'terminal-bench-2/{condition}/{task}/r1',
             'condition': condition, 'task': task, 'job_name': f'{task}-job',
             'status': 'scored', 'rewards': {'reward': reward}, 'repeat': 1}
            for task, reward in [('breached', 1.0), ('clean', 0.0)]
        ],
    }
    record = tmp_path / 'execution.json'
    record.write_text(json.dumps(execution))
    output = tmp_path / 'analysis.json'
    trace = tmp_path / 'trace.jsonl'
    trace.write_text('')
    assert benchmark.main([
        'analyze', '--frozen', str(frozen), '--execution', str(record),
        '--trace', str(trace), '--out', str(output), '--bootstrap', '20',
    ]) == 0
    result = json.loads(output.read_text())
    assert result['counts']['cells_with_operational_value'] == 1
    rows = {row['task']: row[condition] for row in result['per_task_table']}
    for metric in ('operational', 'graded_reward'):
        assert rows['breached'][metric]['value'] is None
        assert rows['breached'][metric]['status'] == 'infrastructure_excluded'
        assert rows['clean'][metric]['value'] == 0.0
    assert result['non_scoring_outcomes'][0]['observed_reward_not_scored'] == 1.0
