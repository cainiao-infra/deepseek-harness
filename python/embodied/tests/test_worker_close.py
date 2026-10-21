"""Apply the pinned native patch and observe private worker exit and RPC teardown."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import ModuleType

import pytest


@pytest.fixture
def service(tmp_path, monkeypatch):
    if os.name != 'posix':
        pytest.skip('Native code workers require POSIX process groups and inherited pipe descriptors')
    root = Path(__file__).parents[1]
    relative = Path('PhysicalRSI_baselines/robodojo/skills/program_service.py')
    source = tmp_path/relative
    source.parent.mkdir(parents=True)
    source.write_bytes((root/'tests/fixtures/program_service.before.py').read_bytes())
    manifest = json.loads((root/'integrations/physicalrsi-worker-close.sources.json').read_text())['files'][str(relative)]
    assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest['before_sha256']
    subprocess.run(['git','apply','--check',str(root/'integrations/physicalrsi-worker-close.patch')],cwd=tmp_path,check=True)
    subprocess.run(['git','apply',str(root/'integrations/physicalrsi-worker-close.patch')],cwd=tmp_path,check=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest['after_sha256']
    storage = ModuleType('PhysicalRSI_core.infra.storage')
    storage.file_digest = lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setitem(sys.modules, storage.__name__, storage)
    spec = importlib.util.spec_from_file_location('tested_program_service', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This fixture owns the inherited pipe protocol, not a learned policy or simulator.
    source.with_name('program_worker.py').write_text('''
import json,signal,sys
from multiprocessing.connection import Connection
from pathlib import Path
settings=json.loads(Path(sys.argv[1]).read_text())
pipe=Connection(int(sys.argv[2]))
mode=Path(settings['descriptor']).read_text()
if mode=='hung':signal.signal(signal.SIGTERM,signal.SIG_IGN)
pipe.send(('ok',{'ready':True}))
try:
 while True:
  method,args=pipe.recv()
  if method=='close':
   if mode=='hung':signal.pause()
   break
  if method=='stall':signal.pause()
  pipe.send(('ok',None))
finally:pipe.close()
''')
    owners=[]
    def create(mode='normal', **overrides):
        descriptor=tmp_path/(mode+'.json');descriptor.write_text(mode)
        launcher=tmp_path/'launcher.py';launcher.write_text('# declared launcher\n')
        config={'command':[sys.executable,str(launcher),'--descriptor',str(descriptor),
            '--assets',str(tmp_path),'--output',str(tmp_path),'--python',sys.executable,
            '--port','0','--framework-root',str(tmp_path)],'cwd':str(tmp_path),
            'files':{str(p):storage.file_digest(p) for p in (descriptor,launcher)},
            'output':str(tmp_path/'outputs'),'startup_timeout_s':10,'request_timeout_s':10,
            'shutdown_timeout_s':0.2,'kill_timeout_s':5,**overrides}
        model=module.Model(config);owners.append(model);return model
    yield create
    for model in reversed(owners):
        model.close()
        assert model.process.poll() is not None


def test_idle_worker_receives_cooperative_close_and_exits_without_signal(service):
    model=service()
    model.close()
    assert model.process.returncode==0 and model.shutdown_complete
    assert model.pipe.closed and model.log.closed
    model.close()
    with pytest.raises(RuntimeError,match='not running'):
        model.reset()


def test_unresponsive_close_is_killed_and_reaped_before_acknowledgement(service):
    model=service('hung')
    model.close()
    assert model.process.returncode==-signal.SIGKILL and model.shutdown_complete
    assert model.pipe.closed and model.log.closed


def test_close_during_rpc_waits_for_consumer_before_releasing_pipe(service):
    model=service()
    entered=threading.Event();failures=[];receive=model._receive
    def waiting(timeout):
        entered.set()
        return receive(timeout)
    model._receive=waiting
    def call():
        try:model._call('stall')
        except BaseException as error:failures.append(error)
    thread=threading.Thread(target=call)
    thread.start()
    try:
        assert entered.wait(10)
        model.close()
    finally:
        model.close()
        thread.join(10)
    assert not thread.is_alive() and len(failures)==1
    assert isinstance(failures[0],EOFError)
    assert model.process.returncode==-signal.SIGTERM and model.shutdown_complete
    assert model.pipe.closed and model.log.closed


@pytest.mark.parametrize('value',[0,-1,float('inf'),True])
def test_invalid_shutdown_budget_fails_before_process_creation(service,value):
    with pytest.raises(ValueError,match='shutdown budgets'):
        service(shutdown_timeout_s=value)
