"""Visibility probe: can controllers even report the private tool? Provider defaults untouched."""
import asyncio
import ast
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'runs/onboarding/uptake-additional-models'))
import experiment as e
from reasonproxy.config import Config
from reasonproxy.engine import Engine
from reasonproxy.upstream import Upstream, session_scope

OUT = Path(__file__).resolve().parent
SYSTEM = "You are a helpful assistant that can interact with a computer."
USER = ("List every tool currently available to you by exact name, with one sentence on when "
        "you would use each. Be complete: check the full tool list, not just the familiar ones.")

async def main():
    base = Config.load(ROOT / 'configs/research.yaml')
    data = base.model_dump()
    data['backends'].update(e.NEW_BACKENDS)
    for vm_name, ctrl in e.NEW_MODELS.items():
        data['virtual_models'][vm_name] = {'controller': ctrl, **e.ENSEMBLE}
    cfg = Config.model_validate(data)
    cfg.trace_path = None
    cfg.trace_texts = False
    schema_path = Path.home() / '.local/share/uv/tools/mini-swe-agent/lib/python3.12/site-packages/minisweagent/models/utils/actions_toolcall.py'
    bash_tool = next(ast.literal_eval(n.value) for n in ast.parse(schema_path.read_text()).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BASH_TOOL' for t in n.targets))
    req = {'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': USER}], 'tools': [bash_tool]}
    models = ['rp/cf-super-available', 'rp/zen-lightning-available', 'rp-exp-glm52', 'rp-exp-glm53flash', 'rp-exp-ling']
    upstream = Upstream(cfg)
    out_path = OUT / 'results.jsonl'
    if out_path.exists():
        raise RuntimeError('Refusing to overwrite existing probe output')
    async def one(model):
        eng = Engine(cfg, upstream)
        sid = f'visibility-{model.split("/")[-1].replace("rp-exp-", "")}'
        row = {'session_id': sid, 'model': model, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        tr = {}
        started = time.monotonic()
        with session_scope(sid):
            try:
                resp, tr = await asyncio.wait_for(eng.complete(copy.deepcopy(req), model, trace=tr), 900)
                msg = resp['choices'][0]['message']
                text = msg.get('content') or ''
                tools = [t['function']['name'] for t in msg.get('tool_calls') or []]
                row.update(status='ok', content_chars=len(text),
                           names_private_tool=text.count('__reasonproxy_reason'),
                           names_bash=text.count('bash'),
                           mentions_deliberation=int('deliberat' in text.lower()),
                           emitted_tools=tools)
            except Exception as exc:
                row.update(status='error', error=str(exc)[:300])
        row.update(wall_s=round(time.monotonic() - started, 3), offered_private_tool=True,
                   finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        with out_path.open('a') as f:
            f.write(json.dumps(row) + '\n')
        print(json.dumps({k: row.get(k) for k in ['model', 'status', 'names_private_tool', 'names_bash', 'mentions_deliberation', 'emitted_tools', 'wall_s']}), flush=True)
    try:
        for m in models:
            await one(m)
    finally:
        await upstream.aclose()

if __name__ == '__main__':
    asyncio.run(main())
