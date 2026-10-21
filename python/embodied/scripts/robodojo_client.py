"""Run native RoboDojo main with the online evaluation consumer in an isolated cwd."""
import argparse
import json
import os
import runpy
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--robodojo', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--hold-first-actions', type=int, default=0)
parser.add_argument('--lose-rgb-after-action', type=int, default=0)
args, native_args = parser.parse_known_args()
args.output.mkdir(parents=True, exist_ok=False)
os.chdir(args.output)
from deepseek_harness_embodied.robodojo import eval_one_episode
from PhysicalRSI_baselines.robodojo import evaluation

def evaluate(TASK_ENV, model_client):
    """Apply declared development faults outside the model-visible recovery input."""
    env = TASK_ENV
    take, observe = env.take_action, env.get_obs
    count, lost = 0, False
    def fault(row):
        with (args.output/'injected-faults.jsonl').open('a') as stream:
            stream.write(json.dumps(row, allow_nan=False)+'\n')
    def action(value):
        nonlocal count
        count += 1
        applied = value
        if count <= args.hold_first_actions:
            state = observe()['state']
            if any(not name.endswith(('arm_joint_state','ee_joint_state')) for name in value):
                raise ValueError('Hold fault only supports joint actions')
            applied = {name:state[name].tolist() if hasattr(state[name], 'tolist') else list(state[name])
                       for name in value}
            fault({'type':'held_command','action_index':count,'requested':value,'applied':applied})
        return take(applied)
    def observation():
        nonlocal lost
        row = observe()
        if args.lose_rgb_after_action > 0 and count == args.lose_rgb_after_action and not lost:
            lost = True
            row = dict(row, vision={})
            fault({'type':'rgb_unavailable','action_index':count})
        return row
    env.take_action, env.get_obs = action, observation
    try:
        return eval_one_episode(env, model_client)
    finally:
        env.take_action, env.get_obs = take, observe

evaluation.eval_one_episode = evaluate
evaluation.eval_one_episode_batch = evaluate
sys.argv = [str(args.robodojo/'src/eval_client/main.py'), *native_args]
runpy.run_path(sys.argv[0], run_name='__main__')
