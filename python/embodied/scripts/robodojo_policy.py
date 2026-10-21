"""Launch the existing RoboDojo WebSocket server with the online PhysicalRSI model."""
import argparse
import asyncio
import json
from pathlib import Path

from client_server.ws.model_server import PolicyServer, PolicyServerConfig
from deepseek_harness_embodied.online_model import OnlineModel
from deepseek_harness_embodied.bridge import write_record

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--deployment', type=Path, required=True)
args = parser.parse_args()
config = json.loads(args.deployment.read_text())
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
