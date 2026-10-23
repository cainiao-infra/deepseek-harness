"""Isolate complete PhysicalRSI catalogs in an owned native WebSocket worker."""
from __future__ import annotations

from copy import deepcopy
import asyncio
import hashlib
import json
import math
import os
import re
from pathlib import Path
import signal
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import psutil

from .bridge import write_record
from .skill_failure import SkillInferenceFailure, decode_result


def bounded_client_type(base):
    """Bound complete native calls, including reconnect, through its loop-thread hook."""
    class BoundedClient(base):
        def __init__(self, **kwargs):
            self.wall_timeout = kwargs['connect_timeout_s']
            self.close_wall_timeout = kwargs['close_timeout_s']
            super().__init__(**kwargs)
            self.wall_timeout = kwargs['request_timeout_s']

        def _run(self, coro):
            if self._loop.is_closed() or not self._loop_thread.is_alive():
                coro.close()
                raise RuntimeError('Native client event loop is not running')
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                return future.result(timeout=self.wall_timeout)
            except BaseException:
                future.cancel()
                raise

        def close(self):
            self.wall_timeout = self.close_wall_timeout
            super().close()
    return BoundedClient


def resolve_worker(value: dict) -> dict:
    """Resolve bounded worker controls before acquiring resources or loading policies."""
    fields = {'python','startup_timeout_seconds','request_timeout_seconds',
              'exit_timeout_seconds','kill_timeout_seconds','connect_timeout_seconds',
              'client_close_timeout_seconds','poll_interval_seconds','capture_execution_diagnostics'}
    if not isinstance(value, dict) or set(value)-fields:
        raise ValueError('Unknown native worker configuration')
    result = {'startup_timeout_seconds':300,'request_timeout_seconds':120,
              'exit_timeout_seconds':10,'kill_timeout_seconds':5,'connect_timeout_seconds':10,
              'client_close_timeout_seconds':5,'poll_interval_seconds':0.05,
              'capture_execution_diagnostics':False, **value}
    executable = Path(result['python'])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('Native worker Python must be an absolute executable path')
    if type(result['capture_execution_diagnostics']) is not bool:
        raise ValueError('Execution diagnostic capture must be boolean')
    for name in fields-{'python', 'capture_execution_diagnostics'}:
        number = result[name]
        if isinstance(number, bool) or not isinstance(number, (int,float)) or not math.isfinite(number) or number <= 0:
            raise ValueError('Positive finite native worker timeouts required')
    return result


class ProcessBinding:
    """Own a single native model process; permit reuse only after acknowledged exit.

    Calls are serialized by the online server. Any RPC, shutdown or metadata
    failure permanently disables this instance; cleanup still attempts to reap
    the root and captured descendants. The caller owns physical stopping.
    """
    def __init__(self, deployment: dict, frozen: dict, settings: dict, root: Path, *, client_factory=None) -> None:
        if client_factory is None:
            from client_server.ws.model_client import WsModelClient
            client_factory = bounded_client_type(WsModelClient)
        self.client_factory, self.settings = client_factory, settings
        self.deployment, self.frozen, self.root = deepcopy(deployment), deepcopy(frozen), root
        self.process = self.client = self.log = None
        self.bound = False
        self.failure: BaseException | None = None
        self.metadata = None
        worker = Path(__file__).with_name('native_worker.py')
        self.identity = {'provider':'owned-native-process','python':settings['python'],
                         'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest()}
        self._start()

    def _start(self) -> None:
        directory = self.root/uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        ready = directory/'ready.json'
        deployment = directory/'deployment.json'
        write_record(deployment, {'ready_file':str(ready),'model':self.deployment,
                     'capture_execution_diagnostics':self.settings['capture_execution_diagnostics']})
        env = {k:v for k,v in os.environ.items()
               if not any(word in k.upper() for word in ('KEY','SECRET','TOKEN','PASSWORD'))}
        env.update(PHYSICALRSI_AGENT_API_KEY='owned-worker-no-inference', PHYSICALRSI_PLANNER_BACKEND='api',
                   PHYSICALRSI_EVAL_OUTPUT=str(directory/'native'), PYTHONUNBUFFERED='1')
        self.log = (directory/'worker.log').open('x')
        try:
            self.process = subprocess.Popen([self.settings['python'],'-m',
                'deepseek_harness_embodied.native_worker','--deployment',str(deployment)],
                stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT,
                env=env,start_new_session=True)
            deadline = time.monotonic()+self.settings['startup_timeout_seconds']
            while not ready.exists():
                if self.process.poll() is not None:
                    raise RuntimeError('Native worker exited before readiness; inspect '+str(directory))
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Native worker readiness deadline exceeded')
                time.sleep(min(remaining,self.settings['poll_interval_seconds']))
            record = json.loads(ready.read_text())
            url = urlsplit(record['url'])
            if record['pid'] != self.process.pid or url.scheme!='ws' or url.hostname!='127.0.0.1' or not url.port:
                raise ValueError('Native worker readiness identity is invalid')
            cfg = self.settings
            self.client = self.client_factory(url=record['url'], evaluation_id=directory.name,
                trial_id=directory.name,connect_timeout_s=cfg['connect_timeout_seconds'],
                handshake_timeout_s=cfg['connect_timeout_seconds'],request_timeout_s=cfg['request_timeout_seconds'],
                max_connect_attempts=1,max_connect_seconds=cfg['connect_timeout_seconds'],
                close_timeout_s=cfg['client_close_timeout_seconds'])
            info = self.client.call(func_name='binding_metadata')
            if (not isinstance(info,dict) or info.get('protocol')!='dsh-native-binding/v2'
                    or not isinstance(info.get('metadata'),dict) or set(info['metadata']) not in ({'config','catalog','operation_program','memory'},
                    {'config','catalog','operation_program','memory','execution_sources'})):
                raise ValueError('Native worker metadata fields are invalid')
            if 'execution_sources' in info['metadata']:
                sources = info['metadata']['execution_sources']
                if (not isinstance(sources,dict) or not sources or any(
                        not isinstance(name,str) or not name or Path(name).name!=name
                        or not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None
                        for name,value in sources.items())):
                    raise ValueError('Invalid native execution source identities')
            if info['pid'] != self.process.pid or info['metadata']['config'] != self.frozen:
                raise ValueError('Native worker returned a different identity or configuration')
            if self.metadata is not None and self.metadata != info['metadata']:
                raise ValueError('Native capability or memory metadata changed after stopping')
            self.metadata = info['metadata']
            write_record(directory/'identity.json', {'provider':self.identity,**info})
        except BaseException as error:
            self.failure = error
            self._dispose(cooperative=False)
            raise

    def _rpc(self, method: str, observation=None, *, operation=None):
        if self.failure is not None:
            raise RuntimeError('Native worker failed; replace the owning service') from self.failure
        try:
            result = self.client.call(func_name=method, obs=observation)
            return decode_result(result, operation) if operation is not None else result
        except BaseException as error:
            self.failure = error
            self.bound = False
            self._dispose(cooperative=False)
            raise

    def _infer(self, method, operation, observation=None):
        result = self._rpc(method, observation, operation=operation)
        if isinstance(result, SkillInferenceFailure):
            self.close()
            raise result
        return result

    def bind(self, plan: dict, observation: dict) -> dict:
        """Bind only after confirmed prior exit and matching native metadata."""
        if self.failure is not None:
            raise RuntimeError('Native worker failed; replace the owning service') from self.failure
        if self.bound:
            raise RuntimeError('Stopping is required before native rebinding')
        if self.process is None:
            self._start()
        result = self._infer('binding_apply', 'bind', {'plan':plan,'observation':observation})
        self.bound = True
        return result

    def observe(self, observation: dict) -> None:
        """Deliver current sensor arrays through RoboDojo's native wire codec."""
        if not self.bound:
            raise RuntimeError('Native observation delivery is disabled while stopped')
        self._infer('binding_observe', 'observe', observation)

    def actions(self) -> list:
        """Fetch native chunks without physical dispatch or conversion to evidence JSON."""
        if not self.bound:
            raise RuntimeError('Native action delivery is disabled while stopped')
        return self._infer('binding_actions', 'actions')

    def close(self) -> None:
        """Require native cleanup acknowledgement, root exit and descendant quiescence."""
        self.bound = False
        try:
            self._dispose(cooperative=self.failure is None)
        except BaseException as error:
            self.failure = error
            raise
        if self.failure is not None:
            raise RuntimeError('Native cleanup outcome is unconfirmed; replace the owning service') from self.failure

    def diagnostics(self) -> dict:
        """Read opt-in policy diagnostics; normal online execution does not call this RPC."""
        if not self.bound:
            raise RuntimeError('Policy diagnostics require a live bound worker')
        result = self._rpc('binding_diagnostics')
        if (not isinstance(result, dict) or result.get('schema') != 'dsh-policy-diagnostics/v1'
                or not isinstance(result.get('versions'), dict) or not isinstance(result.get('loaded_sources'), dict)):
            error = ValueError('Invalid policy diagnostics response')
            self.failure, self.bound = error, False
            self._dispose(cooperative=False)
            raise error
        return result

    def _dispose(self, *, cooperative: bool) -> None:
        process, client = self.process, self.client
        descendants = []
        errors = []
        if process is not None and process.poll() is None:
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess as error:
                # An already exited root needs only its Popen wait below.
                pass
            except (psutil.Error, OSError) as error:
                errors.append(error)
        if cooperative and client is not None:
            try:
                receipt = client.call(func_name='binding_close')
                if receipt != {'closed':True,'pid':process.pid}:
                    raise ValueError('Native close acknowledgement is invalid')
            except BaseException as error:
                errors.append(error)
        if client is not None:
            try:
                client.close()
                if client._loop_thread.is_alive():
                    raise RuntimeError('Native client event loop did not stop')
                self.client = None
            except BaseException as error:
                errors.append(error)
        try:
            exit_deadline = time.monotonic()+self.settings['exit_timeout_seconds']+self.settings['kill_timeout_seconds']
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    code = process.wait(timeout=self.settings['exit_timeout_seconds'])
                except subprocess.TimeoutExpired as error:
                    errors.append(error)
                    os.killpg(process.pid,signal.SIGKILL)
                    code = process.wait(timeout=max(0,exit_deadline-time.monotonic()))
                if cooperative and code != 0:
                    errors.append(RuntimeError('Native worker did not exit normally'))
            _, pending = psutil.wait_procs(descendants,
                timeout=min(self.settings['exit_timeout_seconds'],max(0,exit_deadline-time.monotonic())))
            alive = []
            for child in pending:
                try:
                    if child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:
                        alive.append(child)
                except psutil.NoSuchProcess as error:
                    # This captured descendant exited during the status read.
                    pass
            if alive:
                errors.append(RuntimeError('Native worker left active descendants'))
                for child in alive:
                    try:child.kill()
                    except psutil.NoSuchProcess as error:
                        # This captured descendant exited before the signal.
                        pass
                _, alive = psutil.wait_procs(alive,timeout=max(0,exit_deadline-time.monotonic()))
                if alive:
                    raise RuntimeError('Native descendant exit remains unconfirmed')
        finally:
            if process is None or process.poll() is not None:
                self.process = None
                if self.log is not None:
                    self.log.close()
                    self.log = None
        if errors:
            raise RuntimeError('Native cleanup failed; replacement remains disabled') from errors[0]
