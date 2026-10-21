"""Run isolated native/compatibility/online development episodes, never qualification.

An explicit JSON spec supplies paths, cases, arm order and common limits. The
loopback gateway retains actual provider attempts and usage for every arm. Only
the gateway receives the real API key; child processes receive a local token.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from deepseek_harness_embodied.bridge import write_record
from deepseek_harness_embodied.budget import CallBudget
from deepseek_harness_embodied.evaluation import provider_request, response_usage


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A provider redirect cannot forward credentials to a different origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Gateway:
    """Own a loopback-only, bounded, recorded relay to the official DeepSeek API."""
    def __init__(self, root: Path, spec: dict, key: str) -> None:
        self.root, self.spec, self.key = root, spec, key
        self.token = secrets.token_urlsafe(32)
        self.budget = CallBudget(root/'attempts', spec['calls_per_episode'])
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(NoRedirect())
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.connection.settimeout(spec['provider_timeout_seconds'])
                if (self.path != '/v1/chat/completions'
                        or self.headers.get('Authorization') != 'Bearer '+owner.token):
                    self.send_error(403)
                    return
                if not owner.lock.acquire(blocking=False):
                    self.send_error(409, 'One inference request at a time')
                    return
                slot = None
                receipt = {'status': 'unknown', 'usage': None, 'started': time.monotonic()}
                response_started = False
                captured = bytearray()
                try:
                    size = int(self.headers['Content-Length'])
                    if not 0 < size <= spec['max_request_bytes']:
                        raise ValueError('Provider request size exceeds pilot cap')
                    original = json.loads(self.rfile.read(size))
                    payload = provider_request(original, model=spec['model'], max_tokens=spec['max_tokens'],
                                               reasoning=spec.get('deepseek_reasoning', 'off'))
                    slot = owner.budget.reserve({'received': receipt['started']})
                    directory = root/f'http-{slot:04d}'
                    write_record(directory/'received.json', original)
                    write_record(directory/'sent.json', payload)
                    request = urllib.request.Request('https://api.deepseek.com/chat/completions',
                        data=json.dumps(payload, allow_nan=False).encode(), headers={
                            'Authorization': 'Bearer '+owner.key, 'Content-Type': 'application/json'})
                    with owner.opener.open(request, timeout=spec['provider_timeout_seconds']) as response:
                        receipt['http_status'] = response.status
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        self.end_headers()
                        response_started = True
                        while True:
                            if time.monotonic()-receipt['started'] > spec['provider_timeout_seconds']:
                                raise TimeoutError('Whole provider request deadline exceeded')
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            captured.extend(chunk)
                            if len(captured) > spec['max_response_bytes']:
                                raise ValueError('Provider response exceeds pilot cap')
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    receipt['status'] = 'completed'
                    receipt['usage'] = response_usage(bytes(captured), streaming=bool(payload.get('stream')))
                except Exception as error:
                    receipt['error'] = {'type': type(error).__name__, 'message': str(error)}
                    if not response_started:
                        self.send_error(502, 'Pilot provider request failed; see retained receipt')
                finally:
                    try:
                        if slot is not None:
                            directory = root/f'http-{slot:04d}'
                            (directory/'response.bin').write_bytes(captured)
                            receipt['elapsed_seconds'] = time.monotonic()-receipt['started']
                            receipt['response_sha256'] = hashlib.sha256(captured).hexdigest()
                            write_record(directory/'receipt.json', receipt)
                    finally:
                        owner.lock.release()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def stop(process: subprocess.Popen, grace: float) -> None:
    """Reap the owned process group; signal an exited leader's remaining workers."""
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError as missing_group:
        pass  # The entire owned process group has already exited.
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError as missing_group:
        pass  # No descendant remains in this owned process group.


def episode(spec: dict, case: dict, arm: str, index: int, key: str) -> dict:
    """Retain one attempt, including launch failures; never retry a failed episode."""
    project, dev, repo = (Path(spec[name]) for name in ('project_root', 'deployment_root', 'checkout'))
    out = Path(spec['output'])/f'{index:04d}-{case["case_id"]}-{arm}'
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out/'launcher.py')
    result = {'schema': 'dsh-paired-pilot-episode/v1', 'qualification': False,
              'case': case, 'arm': arm, 'process_timeout': False,
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()}
    write_record(out/'assigned.json', result)
    try:
        with ExitStack() as stack:
            gateway = stack.enter_context(Gateway(out/'provider', spec, key))
            config = json.loads(Path(spec['skill_config']).read_text())
            config['agent'] = {'backend': 'api' if arm == 'N' else 'dsh',
                               'max_tokens': spec['max_tokens']}
            for entry in [*config['skills'].values(), *config['program_registry'].values()]:
                entry['configuration'].update(output=str(out/'skills'),
                    request_timeout_s=spec['skill_request_timeout_seconds'],
                    startup_timeout_s=spec['skill_startup_timeout_seconds'])
            write_record(out/'skills.json', config)
            runtime = json.loads(Path(spec['runtime_config']).read_text())
            runtime.update(run_id=out.name, evidence_root=str(out/'model-evidence'))
            runtime['model_spec'].update(model=spec['model'], base_url=gateway.url,
                max_tokens=spec['max_tokens'], deepseek_reasoning=spec.get('deepseek_reasoning', 'off'))
            write_record(out/'runtime.json', runtime)
            online = json.loads((repo/'python/embodied/configs/robodojo-online.example.json').read_text())
            online.update(evidence_root=str(out/'runner'),
                call_budget={'root': str(out/'turn-reservations'), 'limit': spec['calls_per_episode']},
                episode_timeout_seconds=spec['episode_timeout_seconds'],
                rpc_timeout_seconds=spec['rpc_timeout_seconds'])
            online['recovery']['observation_max_age_seconds'] = spec['rpc_timeout_seconds']
            write_record(out/'online.json', online)
            deployment = {'port': 0, 'ready_file': str(out/'ready.json'),
                          'model': {'task_name': case['task'], 'env_cfg_type': spec['embodiment'], 'action_type': 'joint'}}
            write_record(out/'deployment.json', deployment)
            env = dict(os.environ, PYTHONPATH=':'.join(map(str, [dev/'PhysicalRSI', project/'RoboDojo',
                project/'RoboDojo/XPolicyLab', repo/'python/sdk/src', repo/'python/sdk-runtime/src', repo/'python/embodied/src'])),
                PHYSICALRSI_SKILL_CONFIG=str(out/'skills.json'), PHYSICALRSI_DSH_CONFIG=str(out/'runtime.json'),
                DSH_ROBODOJO_ONLINE_CONFIG=str(out/'online.json'), PHYSICALRSI_EVAL_OUTPUT=str(out/'native-model'),
                PHYSICALRSI_PLANNER_BACKEND='api' if arm == 'N' else 'dsh',
                PHYSICALRSI_AGENT_ENDPOINT=gateway.url+'/chat/completions', PHYSICALRSI_AGENT_MODEL=spec['model'],
                PHYSICALRSI_AGENT_API_KEY=gateway.token, OPENAI_API_KEY=gateway.token, ARK_API_KEY=gateway.token,
                OMNI_KIT_ACCEPT_EULA='YES', OMNI_KIT_ALLOW_ROOT='1', EVAL_NUM='1', ROBODOJO_RUN_ID=out.name)
            mode = 'online' if arm == 'H1' else 'native'
            policy_log = stack.enter_context((out/'policy.log').open('w'))
            policy = subprocess.Popen([str(project/'envs/robodojo-policy/bin/python'), '-u',
                str(repo/'python/embodied/scripts/robodojo_policy.py'), '--mode', mode,
                '--deployment', str(out/'deployment.json')], env=env, stdin=subprocess.DEVNULL, stdout=policy_log,
                stderr=subprocess.STDOUT, start_new_session=True)
            stack.callback(stop, policy, spec['cleanup_grace_seconds'])
            deadline = time.monotonic()+spec['policy_startup_timeout_seconds']
            while not (out/'ready.json').exists():
                if policy.poll() is not None:
                    raise RuntimeError('Policy exited before readiness')
                if time.monotonic() > deadline:
                    raise TimeoutError('Policy readiness deadline exceeded')
                time.sleep(.2)
            url = json.loads((out/'ready.json').read_text())['url']
            client_log = stack.enter_context((out/'client.log').open('w'))
            command = [str(project/'envs/robodojo-sim/bin/python'), '-u',
                str(repo/'python/embodied/scripts/robodojo_client.py'), '--robodojo', str(project/'RoboDojo'),
                '--output', str(out/'simulation'), '--mode', mode,
                '--hold-first-actions', str(case['hold_first_actions']),
                '--lose-rgb-after-action', str(case['lose_rgb_after_action']),
                '--task_name', case['task'], '--env_cfg_type', spec['embodiment'],
                '--num_envs', '1', '--enable_cameras', '--device_id', str(spec['gpu_id']),
                '--policy_name', 'physicalRSICodex', '--port', url.rsplit(':', 1)[1],
                '--host', '127.0.0.1', '--protocol', 'ws', '--policy_server_url', url,
                '--additional_info', out.name, '--seed', str(case['seed']), '--headless',
                '--kit_args=--enable isaacsim.replicator.behavior --enable isaacsim.sensors.camera']
            write_record(out/'launch.json', {'argv': command, 'mode': mode})
            client = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                      stdout=client_log, stderr=subprocess.STDOUT, start_new_session=True)
            stack.callback(stop, client, spec['cleanup_grace_seconds'])
            try:
                result['client_exit'] = client.wait(timeout=spec['process_timeout_seconds'])
            except subprocess.TimeoutExpired:
                result['process_timeout'] = True
                raise
    except Exception as error:
        result['error'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        result['provider_receipts'] = [json.loads(p.read_text()) for p in sorted((out/'provider').glob('http-*/receipt.json'))]
        result['native_results'] = [{'path': str(p.relative_to(out)), 'result': json.loads(p.read_text())}
                                    for p in sorted((out/'simulation').rglob('_result.json'))]
        result['timings'] = [json.loads(line) for p in (out/'simulation').glob('episode-timing.jsonl')
                             for line in p.read_text().splitlines()]
        write_record(out/'result.json', result)
    return result


def main() -> None:
    """Run the predeclared development order without outcome-based stopping."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    validate_spec(spec)
    output = Path(spec['output'])
    output.mkdir(parents=True, exist_ok=False)
    write_record(output/'spec.json', spec)
    key = os.environ['PHYSICALRSI_AGENT_API_KEY']
    index = 0
    for case in spec['cases']:
        for arm in case['order']:
            result = episode(spec, case, arm, index, key)
            print(json.dumps({'index': index, 'case': case['case_id'], 'arm': arm,
                'native_results': result['native_results'], 'error': result.get('error'),
                'provider_attempts': len(result['provider_receipts']), 'qualification': False}), flush=True)
            index += 1


def validate_spec(spec: dict) -> None:
    """Reject incomplete development assignments and unbounded controls before launch."""
    if spec['schema'] != 'dsh-paired-pilot/v1' or spec['partition'] != 'development':
        raise ValueError('Only versioned development pilot specifications are accepted')
    if spec.get('deepseek_reasoning', 'off') not in {'off', 'low', 'high', 'max'}:
        raise ValueError('Unsupported DeepSeek pilot reasoning setting')
    for name in ('project_root', 'deployment_root', 'checkout', 'output', 'skill_config', 'runtime_config'):
        if not isinstance(spec[name], str) or not Path(spec[name]).is_absolute():
            raise ValueError('Absolute pilot paths required')
    for name in ('provider_timeout_seconds', 'skill_request_timeout_seconds', 'skill_startup_timeout_seconds',
                 'episode_timeout_seconds', 'rpc_timeout_seconds', 'cleanup_grace_seconds',
                 'policy_startup_timeout_seconds', 'process_timeout_seconds'):
        value = spec[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Positive finite pilot timeout required')
    for name in ('max_tokens', 'calls_per_episode', 'max_request_bytes', 'max_response_bytes'):
        if type(spec[name]) is not int or spec[name] <= 0:
            raise ValueError('Positive integer pilot cap required')
    if (not spec['cases'] or len({c['case_id'] for c in spec['cases']}) != len(spec['cases'])
            or any(not c['case_id'].isascii() or not c['case_id'].replace('-', '').isalnum()
                   for c in spec['cases'])):
        raise ValueError('Unique safe case identifiers required')
    for case in spec['cases']:
        if sorted(case['order']) != ['H0', 'H1', 'N']:
            raise ValueError('Each pilot case requires all three arms exactly once')
        if any(type(case[name]) is not int or case[name] < 0
               for name in ('seed', 'hold_first_actions', 'lose_rgb_after_action')):
            raise ValueError('Nonnegative integer case controls required')


if __name__ == '__main__':
    main()
