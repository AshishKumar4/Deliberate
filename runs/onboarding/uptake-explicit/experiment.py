"""Diagnostic: explicit user-message instruction to deliberate (compliance, NOT spontaneous uptake).

Two probes, both on the v4 surface with provider defaults untouched:
  quiz       - asks what happens after a deliberate-alone turn. Answerable only
               from the injected system prompt (the tool schema says nothing
               about interception). Tests system-prompt readership.
  compliance - appends an explicit instruction to call deliberate before acting.
               Tests whether uptake is achievable when directly asked, and whether
               the full loop (branches, reduction, checkpoint, action) works.
Neither arm measures spontaneous uptake and neither may be pooled with any
uptake experiment or benchmark condition.
"""
import asyncio
import copy
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location(
    "uptake_v3v4_base", ROOT / 'runs/onboarding/uptake-v3v4/experiment.py')
v3v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3v4)
from reasonproxy.engine import UpstreamError, sanitize_history
from reasonproxy.upstream import Upstream, session_scope

OUT = Path(__file__).resolve().parent
NUDGE = ("Before your next bash action, first call `deliberate` alone (with a focus "
         "question about your current decision, or with no argument) and use the returned "
         "checkpoint to decide your next move. Then act.")
QUIZ = ("Suppose you call `deliberate` alone this turn. What will the caller of this "
        "conversation observe as your response, and what happens next? Answer in two sentences.")
READERSHIP_MARKS = ("intercept", "resum", "nothing", "internal", "caller")

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

async def main():
    cfg, base_sha = v3v4.build_config()
    data = json.loads((ROOT / v3v4.base.SOURCE).read_text())
    messages = [{k: v for k, v in m.items() if k != 'extra'} for m in data['messages']]
    kwargs = data['info']['config']['model']['model_kwargs']
    params = {k: v for k, v in kwargs.items() if k in v3v4.base.PARAMS}
    import ast
    schema_path = Path.home() / '.local/share/uv/tools/mini-swe-agent/lib/python3.12/site-packages/minisweagent/models/utils/actions_toolcall.py'
    bash_tool = next(ast.literal_eval(n.value) for n in ast.parse(schema_path.read_text()).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASH_TOOL' for t in n.targets))
    early = {'messages': messages[:2], 'tools': [bash_tool], **params}
    late = {'messages': messages, 'tools': [bash_tool], **params}
    manifest = {'kind': 'explicit_instruction_diagnostic', 'planned_requests': 25,
                'surface': 'v4 deliberate-focus for every request',
                'source': v3v4.base.SOURCE, 'source_sha256': hashlib.sha256((ROOT / v3v4.base.SOURCE).read_bytes()).hexdigest(),
                'nudge': NUDGE, 'quiz': QUIZ, 'base_config_sha': base_sha,
                'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'limits': '900 seconds per replay; original provider retries and output budgets; no forced tool_choice; provider sampling defaults untouched',
                'method': 'Five controller lanes. Quiz: one early-state request each asking about the deliberate-alone turn. Compliance: early+late states x2 repeats with the nudge appended as a user message. No action is executed; no benchmark reward is measured.',
                'stop_policy': 'Stop a lane on authentication/quota failure or replay timeout; preserve all observations.',
                'warning': 'Compliance measures instruction-following, NOT spontaneous uptake. Never pooled with uptake arms.'}
    (OUT / 'plan.json').write_text(json.dumps(manifest, indent=2) + '\n')
    results_path = OUT / 'results.jsonl'
    if results_path.exists():
        raise RuntimeError('Refusing to overwrite or rerun an existing experiment')
    upstream = Upstream(cfg)

    async def run_request(model, sid, request):
        engine = v3v4.RecordingEngine(cfg, upstream)
        row = {'session_id': sid, 'model': model,
               'input_sha256': digest(request), 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        trace = {}
        started = time.monotonic()
        stop = False
        with session_scope(sid):
            try:
                response, trace = await asyncio.wait_for(engine.complete(copy.deepcopy(request), model, trace=trace), 900)
                message = response['choices'][0]['message']
                tools = message.get('tool_calls') or []
                row['outward_tools'] = [t['function']['name'] for t in tools]
                row['outward_text_chars'] = len(message.get('content') or '')
                row['outward_text'] = message.get('content') or ''
                row['harness_action_valid'] = bool(tools) and all(t['function']['name'] == 'bash' and isinstance(json.loads(t['function']['arguments']).get('command'), str) for t in tools)
                _, _, replayed = sanitize_history([*request['messages'], message])
                row['persisted_checkpoint_replay_count'] = replayed
                row['status'] = 'ok'
            except UpstreamError as exc:
                row.update(status='provider_or_protocol_error', stage=exc.stage, error=exc.detail)
                stop = any(s in exc.detail.lower() for s in ['quota', 'freeusagelimit', 'http 401', 'http 403', 'http 429'])
            except asyncio.TimeoutError:
                row['status'] = 'timeout'
                stop = True
        row.update(wall_s=round(time.monotonic() - started, 3), controller_calls=engine.controller_calls,
                   invocation_positive=int(trace.get('reason_calls', 0) > 0),
                   trace_reason_calls=trace.get('reason_calls', 0), branches=len(trace.get('branches', [])),
                   usable_branches=sum(bool(b.get('usable')) for b in trace.get('branches', [])),
                   accepted_reductions=sum(bool(r.get('accepted')) for r in trace.get('reducer', [])),
                   focus_chars=sum(c.get('focus_chars', 0) for c in trace.get('checkpoints', [])),
                   prompt_tool_id=(trace.get('prompt_revisions') or {}).get('reason_tool', {}).get('id'),
                   protocol_violations=trace.get('protocol_violations', []), degraded=trace.get('degraded'),
                   usage=trace.get('usage_totals'), finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        with results_path.open('a') as f:
            f.write(json.dumps(row) + '\n')
        return row, stop

    async def lane(slug):
        model = f'exp-{slug}-v4'
        quiz_req = {**copy.deepcopy(early), 'messages': [*early['messages'], {'role': 'user', 'content': QUIZ}]}
        row, stop = await run_request(model, f'uptake-explicit-{slug}-quiz', quiz_req)
        print(json.dumps({'controller': slug, 'probe': 'quiz', 'status': row['status'], 'wall_s': row['wall_s']}), flush=True)
        if stop:
            print(json.dumps({'lane_stopped': slug}), flush=True)
            return
        for state_index, (state, base_req) in enumerate([('early', early), ('late', late)]):
            for repeat in range(2):
                req = copy.deepcopy(base_req)
                req['messages'] = [*req['messages'], {'role': 'user', 'content': NUDGE}]
                row, stop = await run_request(model, f'uptake-explicit-{slug}-{state}-{repeat}-nudge', req)
                print(json.dumps({'controller': slug, 'probe': 'compliance', 'state': state, 'repeat': repeat,
                                  'status': row['status'], 'invocation_positive': row.get('invocation_positive', 0),
                                  'focus_chars': row.get('focus_chars', 0), 'wall_s': row['wall_s']}), flush=True)
                if stop:
                    print(json.dumps({'lane_stopped': slug}), flush=True)
                    return
    try:
        await asyncio.gather(*(lane(s) for s in v3v4.CONTROLLERS))
    finally:
        await upstream.aclose()

if __name__ == '__main__':
    asyncio.run(main())
