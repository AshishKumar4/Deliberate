"""Retry the transient-failed CF lanes with identical matched design; new output file."""
import asyncio
import copy
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location("uptake_v3v4_base", ROOT / 'runs/onboarding/uptake-v3v4/experiment.py')
v3v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3v4)
from reasonproxy.engine import UpstreamError, sanitize_history
from reasonproxy.upstream import Upstream, session_scope

OUT = Path(__file__).resolve().parent
LANES = ['cf-super', 'glm52', 'glm53flash']

async def main():
    cfg, _ = v3v4.build_config()
    import ast
    schema_path = Path.home() / '.local/share/uv/tools/mini-swe-agent/lib/python3.12/site-packages/minisweagent/models/utils/actions_toolcall.py'
    bash_tool = next(ast.literal_eval(n.value) for n in ast.parse(schema_path.read_text()).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASH_TOOL' for t in n.targets))
    data = json.loads((ROOT / v3v4.base.SOURCE).read_text())
    messages = [{k: v for k, v in m.items() if k != 'extra'} for m in data['messages']]
    kwargs = data['info']['config']['model']['model_kwargs']
    params = {k: v for k, v in kwargs.items() if k in v3v4.base.PARAMS}
    states = {'early': {'messages': messages[:2], 'tools': [bash_tool], **params},
              'late': {'messages': messages, 'tools': [bash_tool], **params}}
    results_path = OUT / 'results-retry-cf.jsonl'
    if results_path.exists():
        raise RuntimeError('Refusing to overwrite existing retry output')
    upstream = Upstream(cfg)
    order_cycle = ['v2', 'v3', 'v4']
    async def lane(slug):
        for state_index, (state, request) in enumerate(states.items()):
            for repeat in range(3):
                rot = (repeat + state_index) % 3
                order = order_cycle[rot:] + order_cycle[:rot]
                for version in order:
                    model = f'exp-{slug}-{version}'
                    engine = v3v4.RecordingEngine(cfg, upstream)
                    sid = f'uptake-v3v4-retry-{slug}-{state}-{repeat}-{version}'
                    row = {'session_id': sid, 'controller': slug, 'model': model, 'version': version,
                           'state': state, 'repeat': repeat, 'input_sha256': v3v4.digest(request),
                           'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                    trace = {}
                    started = time.monotonic()
                    stop = False
                    with session_scope(sid):
                        try:
                            response, trace = await asyncio.wait_for(engine.complete(copy.deepcopy(request), model, trace=trace), 900)
                            message = response['choices'][0]['message']
                            tools = message.get('tool_calls') or []
                            row['outward_tools'] = [t['function']['name'] for t in tools]
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
                    print(json.dumps({k: row.get(k) for k in ['controller', 'state', 'repeat', 'version', 'status', 'invocation_positive', 'branches', 'accepted_reductions', 'focus_chars', 'harness_action_valid', 'wall_s']}), flush=True)
                    if stop:
                        print(json.dumps({'lane_stopped': slug, 'session_id': sid}), flush=True)
                        return
    try:
        await asyncio.gather(*(lane(s) for s in LANES))
    finally:
        await upstream.aclose()

if __name__ == '__main__':
    asyncio.run(main())
