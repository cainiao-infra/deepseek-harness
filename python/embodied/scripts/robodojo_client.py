"""Run RoboDojo main with native or online evaluation and passive pilot records."""
import argparse
import hashlib
import json
import os
import runpy
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--robodojo', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--hold-first-actions', type=int, default=0)
parser.add_argument('--lose-rgb-after-action', type=int, default=0)
parser.add_argument('--mode', choices=('online', 'native'), default='online')
parser.add_argument('--layout-id', type=int)
parser.add_argument('--layout-sha256')
parser.add_argument('--fresh-layout-manifest', type=Path)
parser.add_argument('--fresh-layout-manifest-sha256')
parser.add_argument('--fresh-layout-task')
parser.add_argument('--fresh-layout-index', type=int)
parser.add_argument('--fresh-layout-split', choices=('development','validation'))
parser.add_argument('--comparison-sha256')
args, native_args = parser.parse_known_args()
if (args.layout_id is None) != (args.layout_sha256 is None):
    parser.error('--layout-id and --layout-sha256 must be supplied together')
fresh_args = (args.fresh_layout_manifest, args.fresh_layout_manifest_sha256,
              args.fresh_layout_task, args.fresh_layout_index, args.fresh_layout_split)
if any(value is not None for value in fresh_args) and (any(value is None for value in fresh_args) or args.layout_id != 0):
    parser.error('Fresh layouts require a manifest, task, cohort index, split and --layout-id 0')
args.output.mkdir(parents=True, exist_ok=False)
os.chdir(args.output)
from deepseek_harness_embodied.robodojo import eval_one_episode
from deepseek_harness_embodied.online_config import load_online_config
from deepseek_harness_embodied.terminal_timing import TerminalTiming
from deepseek_harness_embodied.layouts import bind_fresh_case, select_layout
from PhysicalRSI_baselines.robodojo import evaluation
from client_server.ws import model_client as native_client

settings = load_online_config(Path(os.environ['DSH_ROBODOJO_ONLINE_CONFIG']))
native_evaluate = evaluation.eval_one_episode
if args.layout_id is not None:
    from env.seed_manager import seed_manager
    SeedManager = seed_manager.SeedManager
    fresh_source = None
    if args.fresh_layout_manifest is not None:
        SeedManager, fresh_source = bind_fresh_case(SeedManager,
            manifest_path=args.fresh_layout_manifest, task=args.fresh_layout_task,
            index=args.fresh_layout_index, expected_sha256=args.layout_sha256,
            expected_manifest_sha256=args.fresh_layout_manifest_sha256,
            split=args.fresh_layout_split, comparison_sha256=args.comparison_sha256)
        seed_manager.SeedManager = SeedManager
    native_init_eval = SeedManager.init_eval
    def selected_init_eval(manager, *init_args, **init_kwargs):
        native_init_eval(manager, *init_args, **init_kwargs)
        select_layout(manager, layout_id=args.layout_id, expected_sha256=args.layout_sha256,
                      evidence=args.output/'selected-layout.json', fresh_source=fresh_source)
    SeedManager.init_eval = selected_init_eval
client_class = native_client.WsModelClient
def configured_client(**kwargs):
    """Construct new native clients with the deployment's whole-RPC budget."""
    return client_class(**kwargs, request_timeout_s=settings['rpc_timeout_seconds'])
native_client.WsModelClient = configured_client

def evaluate(TASK_ENV, model_client):
    """Apply declared development faults outside the model-visible recovery input."""
    env = TASK_ENV
    take, observe, check_end = env.take_action, env.get_obs, env.is_episode_end
    count, lost = 0, False
    started = time.monotonic()
    initial = None
    error = None
    terminal_timing = TerminalTiming()
    def check_deadline():
        if time.monotonic() - started >= settings['episode_timeout_seconds']:
            raise TimeoutError('Common evaluation episode deadline exceeded')
    def fault(row):
        with (args.output/'injected-faults.jsonl').open('a') as stream:
            stream.write(json.dumps(row, allow_nan=False)+'\n')
    def action(value):
        nonlocal count
        check_deadline()
        count += 1
        applied = value
        if count <= args.hold_first_actions:
            state = observe()['state']
            if any(not name.endswith(('arm_joint_state','ee_joint_state')) for name in value):
                raise ValueError('Hold fault only supports joint actions')
            applied = {name:state[name].tolist() if hasattr(state[name], 'tolist') else list(state[name])
                       for name in value}
            fault({'type':'held_command','action_index':count,'requested':value,'applied':applied})
        terminal_timing.dispatched()
        return take(applied)
    def terminal():
        ended = check_end()
        terminal_timing.checked(bool(ended), bool(env.success[0]))
        return ended
    def observation():
        nonlocal lost, initial
        check_deadline()
        row = observe()
        if initial is None:
            state = {name: value.tolist() if hasattr(value, 'tolist') else value
                     for name, value in row.get('state', {}).items()}
            cameras = {name: {
                'shape': list(camera['color'].shape),
                'sha256': hashlib.sha256(camera['color'].tobytes()).hexdigest()}
                for name, camera in row.get('vision', {}).items()
                if camera.get('color') is not None}
            initial = {'instruction': row.get('instruction'), 'state': state, 'cameras': cameras}
            (args.output/'initial-observation.json').write_text(json.dumps(initial, allow_nan=False))
        if args.lose_rgb_after_action > 0 and count == args.lose_rgb_after_action and not lost:
            lost = True
            row = dict(row, vision={})
            fault({'type':'rgb_unavailable','action_index':count})
        return row
    env.take_action, env.get_obs, env.is_episode_end = action, observation, terminal
    try:
        evaluate_episode = native_evaluate if args.mode == 'native' else eval_one_episode
        return evaluate_episode(env, model_client)
    except BaseException as failure:
        error = {'type': type(failure).__name__, 'message': str(failure)}
        raise
    finally:
        env.take_action, env.get_obs, env.is_episode_end = take, observe, check_end
        (args.output/'native-terminal.json').write_text(json.dumps(terminal_timing.snapshot(), allow_nan=False)+'\n')
        record = {'schema': 'dsh-robodojo-pilot-timing/v1', 'mode': args.mode,
                  'started': started, 'elapsed_seconds': time.monotonic()-started,
                  'actions': count, 'error': error,
                  'qualification': False, 'includes_skill_cold_start': True}
        with (args.output/'episode-timing.jsonl').open('a') as stream:
            stream.write(json.dumps(record, allow_nan=False)+'\n')

evaluation.eval_one_episode = evaluate
evaluation.eval_one_episode_batch = evaluate
sys.argv = [str(args.robodojo/'src/eval_client/main.py'), *native_args]
runpy.run_path(sys.argv[0], run_name='__main__')
