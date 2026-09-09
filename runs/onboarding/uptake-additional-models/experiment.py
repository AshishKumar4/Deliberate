"""Unscored matched replay for additional controllers; never executes actions."""
import asyncio
import ast
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from reasonproxy.config import Config
from reasonproxy.engine import Engine, UpstreamError, sanitize_history
from reasonproxy.prompts import CONTROLLER_PROMPTS, REASON_WIRE_NAME
from reasonproxy.upstream import Upstream, session_scope

OUT = Path(__file__).resolve().parent
CLARIFICATION = ('\nA turn containing only __reasonproxy_reason is an internal runtime turn: '
                 'the proxy intercepts it and resumes you before returning any response to the caller. '
                 "The caller's requirement to include a bash action applies to the eventual outward "
                 'response, not to these internal deliberation turns. After internal deliberation, '
                 "return the action required by the caller. This does not require using deliberation "
                 'on any particular turn; use it only when warranted.')
# Fixed caller states: Lightning sqlite trajectory, early + late. Only controller varies.
SOURCE = 'runs/tb2-guarded/terminal-bench-2__rp-zen-lightning-available__sqlite-db-truncate__r1__20260907T172028Z-b3dd14b0/sqlite-db-truncate__Eq2igmb/agent/mini-swe-agent.trajectory.json'
PARAMS = {'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'presence_penalty', 'frequency_penalty', 'seed', 'parallel_tool_calls'}
NEW_BACKENDS = {
    'ctrl-exp-glm52': {'provider': 'cf', 'model': '@cf/zai-org/glm-5.2', 'params': {'temperature': 0.0}},
    'ctrl-exp-glm53flash': {'provider': 'cf', 'model': '@cf/zai-org/glm-5.3-flash', 'params': {'temperature': 0.0}},
    'ctrl-exp-ling': {'provider': 'zen', 'model': 'ling-3.0-flash-fin-free', 'params': {'temperature': 0.0}},
}
NEW_MODELS = {
    'rp-exp-glm52': 'ctrl-exp-glm52',
    'rp-exp-glm53flash': 'ctrl-exp-glm53flash',
    'rp-exp-ling': 'ctrl-exp-ling',
}
ENSEMBLE = {'branches': ['br-cf-super', 'br-cf-super-hot', 'br-zen-lightning', 'br-zen-lightning-hot'],
            'reducer': 'red-cf-super', 'min_branches': 4, 'max_reason_calls': 2,
            'branch_timeout_s': 420, 'persistence': 'assistant_tags', 'controller_prompt': 'concise',
            'reason_mode': 'live'}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

class ReplayEngine(Engine):
    def __init__(self, cfg, upstream, clarified):
        super().__init__(cfg, upstream)
        self.clarified = clarified
        self.controller_calls = []

    def _inject(self, messages, vm):
        result = super()._inject(messages, vm)
        index = 0
        while index < len(messages) and messages[index].get('role') in {'system', 'developer'}:
            index += 1
        if self.clarified:
            result[index] = {**result[index], 'content': result[index]['content'] + CLARIFICATION}
        return result

    async def _controller(self, vm, messages, tools, tool_choice, overrides, usage, tr):
        offered = [t['function']['name'] for t in tools or []]
        out = await super()._controller(vm, messages, tools, tool_choice, overrides, usage, tr)
        names = [t['function']['name'] for t in out.tool_calls]
        valid_private = 0
        if names == [REASON_WIRE_NAME]:
            args = out.tool_calls[0]['function'].get('arguments', '')
            try:
                valid_private = int(json.loads(args or '{}') == {})
            except (TypeError, ValueError):
                pass
        self.controller_calls.append({'offered_tools': offered, 'tool_choice': tool_choice,
                                      'emitted_tools': names, 'valid_private_only_call': valid_private})
        return out

def build_config():
    cfg = Config.load(ROOT / 'configs/research.yaml')
    cfg.trace_path = None
    cfg.trace_texts = False
    data = cfg.model_dump()
    data['backends'].update(NEW_BACKENDS)
    for vm_name, ctrl in NEW_MODELS.items():
        data['virtual_models'][vm_name] = {'controller': ctrl, **ENSEMBLE}
    return Config.model_validate(data), cfg.sha

async def main():
    cfg, base_sha = build_config()
    schema_path = Path.home() / '.local/share/uv/tools/mini-swe-agent/lib/python3.12/site-packages/minisweagent/models/utils/actions_toolcall.py'
    schema_ast = ast.parse(schema_path.read_text())
    bash_tool = next(ast.literal_eval(n.value) for n in schema_ast.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASH_TOOL' for t in n.targets))
    data = json.loads((ROOT / SOURCE).read_text())
    messages = [{k: v for k, v in m.items() if k != 'extra'} for m in data['messages']]
    assert messages[0]['role'] == 'system' and messages[1]['role'] == 'user' and messages[-1]['role'] == 'tool'
    kwargs = data['info']['config']['model']['model_kwargs']
    params = {k: v for k, v in kwargs.items() if k in PARAMS}
    states = {'early': {'messages': messages[:2], 'tools': [bash_tool], **params},
              'late': {'messages': messages, 'tools': [bash_tool], **params}}
    manifest = {'kind': 'unscored_matched_controller_replay', 'planned_requests': 36,
                'models': list(NEW_MODELS), 'repeats_per_arm_state_model': 3,
                'source': SOURCE, 'source_sha256': hashlib.sha256((ROOT / SOURCE).read_bytes()).hexdigest(),
                'clarification': CLARIFICATION, 'base_prompt': CONTROLLER_PROMPTS['concise'][1],
                'base_config_sha': base_sha, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'requests': {name: {'sha256': digest(req), 'messages': len(req['messages'])} for name, req in states.items()},
                'limits': '900 seconds per replay; original provider retries and output budgets; no forced tool_choice',
                'method': 'Three model lanes concurrently; fixed Lightning caller states, three repetitions, counterbalanced original/clarified order. Only controller backend varies; ensemble fixed to available-group roster. No proposed action is executed; no benchmark reward is measured.',
                'stop_policy': 'Stop a lane on authentication/quota failure or replay timeout; preserve all observations. Do not adapt sample size to uptake.'}
    (OUT / 'plan.json').write_text(json.dumps(manifest, indent=2) + '\n')
    results_path = OUT / 'results.jsonl'
    if results_path.exists():
        raise RuntimeError('Refusing to overwrite or rerun an existing experiment')
    upstream = Upstream(cfg)
    async def lane(model):
        for state_index, (state, request) in enumerate(states.items()):
            for repeat in range(3):
                order = ['original', 'clarified'] if (repeat + state_index) % 2 == 0 else ['clarified', 'original']
                for arm in order:
                    engine = ReplayEngine(cfg, upstream, arm == 'clarified')
                    sid = f'uptake-additional-{model.split("-")[-1]}-{state}-{repeat}-{arm}'
                    row = {'session_id': sid, 'model': model, 'state': state, 'repeat': repeat, 'arm': arm,
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
        await asyncio.gather(*(lane(model) for model in NEW_MODELS))
    finally:
        await upstream.aclose()

if __name__ == '__main__':
    asyncio.run(main())
