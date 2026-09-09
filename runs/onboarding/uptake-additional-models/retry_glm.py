"""Retry transient-failed GLM lanes with identical matched design; new output file."""
import asyncio
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment as e
from reasonproxy.engine import UpstreamError, sanitize_history
from reasonproxy.upstream import Upstream, session_scope

OUT = Path(__file__).resolve().parent
MODELS = ['rp-exp-glm52', 'rp-exp-glm53flash']

async def main():
    cfg, _ = e.build_config()
    import ast
    schema_path = Path.home() / '.local/share/uv/tools/mini-swe-agent/lib/python3.12/site-packages/minisweagent/models/utils/actions_toolcall.py'
    bash_tool = next(ast.literal_eval(n.value) for n in ast.parse(schema_path.read_text()).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASH_TOOL' for t in n.targets))
    data = json.loads((Path(e.ROOT) / e.SOURCE).read_text())
    messages = [{k: v for k, v in m.items() if k != 'extra'} for m in data['messages']]
    import re
    kwargs = data['info']['config']['model']['model_kwargs']
    params = {k: v for k, v in kwargs.items() if k in e.PARAMS}
    states = {'early': {'messages': messages[:2], 'tools': [bash_tool], **params},
              'late': {'messages': messages, 'tools': [bash_tool], **params}}
    results_path = OUT / 'results-retry-glm.jsonl'
    if results_path.exists():
        raise RuntimeError('Refusing to overwrite existing retry output')
    upstream = Upstream(cfg)
    async def lane(model):
        for state_index, (state, request) in enumerate(states.items()):
            for repeat in range(3):
                order = ['original', 'clarified'] if (repeat + state_index) % 2 == 0 else ['clarified', 'original']
                for arm in order:
                    engine = e.ReplayEngine(cfg, upstream, arm == 'clarified')
                    sid = f'uptake-additional-retry-{model.split("-")[-1]}-{state}-{repeat}-{arm}'
                    row = {'session_id': sid, 'model': model, 'state': state, 'repeat': repeat, 'arm': arm,
                           'input_sha256': e.digest(request), 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
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
                               valid_private_invocations=sum(c['valid_private_only_call'] for c in engine.controller_calls),
                               trace_reason_calls=trace.get('reason_calls', 0), branches=len(trace.get('branches', [])),
                               usable_branches=sum(bool(b.get('usable')) for b in trace.get('branches', [])),
                               accepted_reductions=sum(bool(r.get('accepted')) for r in trace.get('reducer', [])),
                               protocol_violations=trace.get('protocol_violations', []), degraded=trace.get('degraded'),
                               usage=trace.get('usage_totals'), finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
                    with results_path.open('a') as f:
                        f.write(json.dumps(row) + '\n')
                    print(json.dumps({k: row.get(k) for k in ['model', 'state', 'repeat', 'arm', 'status', 'valid_private_invocations', 'branches', 'accepted_reductions', 'harness_action_valid', 'wall_s']}), flush=True)
                    if stop:
                        print(json.dumps({'lane_stopped': model, 'session_id': sid}), flush=True)
                        return
    try:
        await asyncio.gather(*(lane(m) for m in MODELS))
    finally:
        await upstream.aclose()

if __name__ == '__main__':
    asyncio.run(main())
