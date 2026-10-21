"""Owned policy process lifecycle with a recorded transport and real child exits."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from deepseek_harness_embodied import process_binding as module
from deepseek_harness_embodied.native_worker import WorkerModel
from test_native_binding import factory


@pytest.fixture
def worker(tmp_path,monkeypatch):
    if os.name!='posix':
        pytest.skip('Native worker lifecycle uses POSIX process groups')
    script=tmp_path/'child.py'
    script.write_text('''
import json,os,signal,sys,time
from pathlib import Path
config=json.loads(Path(sys.argv[1]).read_text())
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
ready=Path(config['ready_file'])
with ready.with_suffix('.temporary').open('x') as f:json.dump({'pid':os.getpid(),'url':'ws://127.0.0.1:1'},f)
os.replace(ready.with_suffix('.temporary'),ready)
while True:signal.pause()
''')
    children=[];clients=[];commands=[];mode={'fault':None};create=subprocess.Popen
    def launch(args,**kwargs):
        commands.append((args,kwargs['env']))
        child=create([sys.executable,str(script),args[-1]],**kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    monkeypatch.setenv('EXAMPLE_PRIVATE_TOKEN','must-not-reach-worker')
    frozen={'skills':{'pi05':{'implementation':'pi05'}}}
    info={'config':frozen,'catalog':{'learned':{}},'memory':{},'operation_program':None}
    class Client:
        def __init__(self,**kwargs):
            self.pid=children[-1].pid;self.closed=False
            self._loop_thread=SimpleNamespace(is_alive=lambda:not self.closed)
            clients.append(self)
        def call(self,func_name,obs=None):
            if mode['fault']==func_name:raise TimeoutError('recorded RPC failure')
            if func_name=='binding_metadata':return {'pid':self.pid,'metadata':deepcopy(info)}
            if func_name=='binding_apply':return {'native_root':'root','skill':{'name':'learned'}}
            if func_name=='binding_actions':return [{'joint':np.array([0.1])}]
            if func_name=='binding_close':return {'closed':True,'pid':self.pid}
        def close(self):self.closed=True
    settings=module.resolve_worker({'python':sys.executable,'startup_timeout_seconds':10,
        'exit_timeout_seconds':5,'kill_timeout_seconds':5})
    binding=module.ProcessBinding({},frozen,settings,tmp_path/'workers',client_factory=Client)
    try:yield binding,children,clients,mode,info,commands
    finally:
        mode['fault']=None
        try:
            try:binding.close()
            except RuntimeError:
                # A poisoned provider must remain failed after resources are released.
                assert binding.failure is not None
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill();child.wait(timeout=10)
        assert all(c.closed for c in clients)


def test_owned_worker_rebind_requires_prior_exit_and_preserves_arrays(worker):
    binding,children,clients,mode,info,commands=worker
    binding.bind({'composition':'learned'},{'state':{}})
    assert isinstance(binding.actions()[0]['joint'],np.ndarray)
    with pytest.raises(RuntimeError,match='Stopping'):
        binding.bind({}, {})
    binding.close()
    assert children[0].returncode==0 and clients[0].closed
    with pytest.raises(RuntimeError,match='stopped'):binding.actions()
    binding.bind({'composition':'learned'},{'state':{}})
    assert len(children)==2 and children[0].pid!=children[1].pid
    assert 'EXAMPLE_PRIVATE_TOKEN' not in commands[0][1]
    assert commands[0][0][1:3]==['-m','deepseek_harness_embodied.native_worker']


def test_worker_metadata_change_reaps_replacement_before_actions(worker):
    binding,children,clients,mode,info,commands=worker
    binding.close();info['memory']['changed']=True
    with pytest.raises(ValueError,match='metadata changed'):
        binding.bind({}, {})
    assert all(child.poll() is not None for child in children)
    with pytest.raises(RuntimeError,match='failed'):binding.bind({}, {})


@pytest.mark.parametrize('method',['binding_apply','binding_actions','binding_close'])
def test_unknown_rpc_outcome_reaps_process_and_permanently_blocks_reuse(worker,method):
    binding,children,clients,mode,info,commands=worker
    if method!='binding_apply':binding.bind({}, {})
    mode['fault']=method
    with pytest.raises((TimeoutError,RuntimeError)):
        if method=='binding_apply':binding.bind({}, {})
        elif method=='binding_actions':binding.actions()
        else:binding.close()
    assert children[0].poll() is not None and clients[0].closed
    with pytest.raises(RuntimeError,match='failed'):binding.bind({}, {})
    with pytest.raises(RuntimeError,match='unconfirmed'):binding.close()


def test_worker_consumes_admitted_plan_and_close_is_terminal():
    models=[];worker=WorkerModel(factory(models),{})
    plan={'composition':'pick'}
    worker.binding_apply({'plan':plan,'observation':{'instruction':'pick'}})
    assert models[0].chosen==plan
    assert worker.binding_actions()==[{'joint':[1]}]
    assert worker.binding_close()=={'closed':True,'pid':os.getpid()}
    with pytest.raises(RuntimeError,match='cannot be rebound'):
        worker.binding_apply({'plan':plan,'observation':{}})


@pytest.mark.parametrize('change',[{'python':'relative-python'}, {'startup_timeout_seconds':0},
    {'request_timeout_seconds':True},{'poll_interval_seconds':float('nan')},{'unknown':1}])
def test_worker_configuration_rejects_unbounded_or_implicit_launch(change):
    with pytest.raises(ValueError):module.resolve_worker({'python':sys.executable,**change})


def test_native_observation_failure_closes_service_before_any_later_actions():
    models=[];worker=WorkerModel(factory(models),{})
    worker.binding_apply({'plan':{'composition':'pick'},'observation':{'instruction':'pick'}})
    def fail(*args):raise ValueError('native observation failure')
    models[0].update_obs_batch=fail
    with pytest.raises(ValueError,match='observation failure'):worker.binding_observe({})
    assert models[0].closed
    with pytest.raises(RuntimeError,match='stopped'):worker.binding_actions()


def test_descendant_inspection_failure_still_reaps_the_owned_root(worker,monkeypatch):
    binding,children,clients,*_=worker
    def denied(*args,**kwargs):raise PermissionError('process enumeration denied')
    monkeypatch.setattr(module.psutil,'Process',denied)
    with pytest.raises(RuntimeError,match='cleanup failed'):binding.close()
    assert children[0].poll() is not None and clients[0].closed
    with pytest.raises(RuntimeError,match='failed'):binding.bind({}, {})


def test_online_worker_timeouts_include_startup_and_cleanup(tmp_path):
    from deepseek_harness_embodied.online_config import load_online_config
    source=Path(__file__).parents[1]/'configs/robodojo-online.example.json'
    config=json.loads(source.read_text());config['native_worker']={'python':sys.executable}
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='stop timeout'):load_online_config(path)
    config['recovery']['stop_timeout_seconds']=150;path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='RPC timeout'):load_online_config(path)
    config['rpc_timeout_seconds']=600;path.write_text(json.dumps(config))
    assert load_online_config(path)['native_worker']['startup_timeout_seconds']==300


def test_unexpected_worker_death_is_not_a_successful_stop(worker):
    binding,children,clients,*_=worker
    children[0].kill();children[0].wait(timeout=10)
    with pytest.raises(RuntimeError,match='cleanup failed'):binding.close()
    assert clients[0].closed and binding.process is None
    with pytest.raises(RuntimeError,match='failed'):binding.bind({}, {})


def test_wrong_close_acknowledgement_still_reaps_root_and_refuses_reuse(worker):
    binding,children,clients,*_=worker
    original=clients[0].call
    clients[0].call=lambda func_name,obs=None:({'closed':True,'pid':-1} if func_name=='binding_close'
                                             else original(func_name,obs))
    with pytest.raises(RuntimeError,match='cleanup failed'):binding.close()
    assert children[0].poll() is not None and clients[0].closed
    with pytest.raises(RuntimeError,match='failed'):binding.bind({}, {})


def test_whole_rpc_deadline_cancels_pending_native_coroutine_before_loop_release():
    import asyncio
    import threading
    class NativeClient:
        def __init__(self,**kwargs):
            self._loop=asyncio.new_event_loop()
            self._loop_thread=threading.Thread(target=self._loop.run_forever)
            self._loop_thread.start()
        def close(self):
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(10)
            assert not self._loop_thread.is_alive()
            self._loop.close()
    client=module.bounded_client_type(NativeClient)(connect_timeout_s=1,
        close_timeout_s=1,request_timeout_s=0.05)
    entered=threading.Event();cancelled=threading.Event()
    async def blocked():
        entered.set()
        try:await asyncio.Future()
        finally:cancelled.set()
    try:
        with pytest.raises(TimeoutError):client._run(blocked())
        async def drained():
            tasks=[t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            await asyncio.gather(*tasks,return_exceptions=True)
            return all(t.done() for t in tasks)
        assert asyncio.run_coroutine_threadsafe(drained(),client._loop).result(timeout=10)
        assert not entered.is_set() or cancelled.is_set()
    finally:client.close()
