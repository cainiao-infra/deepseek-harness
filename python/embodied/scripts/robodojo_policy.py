"""Launch RoboDojo's loopback server with native or online PhysicalRSI execution."""
import argparse
import asyncio
import json
from pathlib import Path

from client_server.ws.model_server import PolicyServer, PolicyServerConfig
from deepseek_harness_embodied.online_model import OnlineModel
from deepseek_harness_embodied.bridge import write_record

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--deployment', type=Path, required=True)
parser.add_argument('--mode', choices=('online', 'native'), default='online')
args = parser.parse_args()
config = json.loads(args.deployment.read_text())
if args.mode == 'native':
    from PhysicalRSI_baselines.robodojo.skill_model import Model
    if 'execution_diagnostics' in config:
        from deepseek_harness_embodied.policy_diagnostics import diagnostic_model_type
        Model = diagnostic_model_type(Model, Path(config['execution_diagnostics']))
    model = Model(config['model'])
    try:
        if 'initial_plan' in config:
            from deepseek_harness_embodied.native_binding import install_initial_plan
            plan = install_initial_plan(model, config['initial_plan'])
            write_record(Path(config['ready_file']).with_name('configured-initial-plan.json'),
                         {'origin':'deployment','plan':plan,'model_calls':0})
    except BaseException:
        model.close()
        raise
else:
    model = OnlineModel(config['model'])
server = PolicyServer(model, PolicyServerConfig(host='127.0.0.1', port=config['port']))

async def serve():
    await server.start()
    write_record(Path(config['ready_file']), {'url':server.url})
    await server.serve_forever()

try:
    asyncio.run(serve())
finally:
    model.close()
