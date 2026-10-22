"""Owned native policy server using RoboDojo's existing WebSocket protocol.

Only committed plans arrive here. The DSH planner and its Session remain in the
parent; process exit releases learned-policy device state after native cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal

from .bridge import write_record
from .native_binding import OwnedModelBinding
from .skill_failure import SkillInferenceFailure


class WorkerModel:
    """Expose native binding operations without another planning inference path."""
    def __init__(self, factory, deployment: dict) -> None:
        self.binding = OwnedModelBinding(factory, deployment)
        self.closed = False

    def binding_metadata(self) -> dict:
        """Return the frozen capability snapshot and actual child process identity."""
        return {'protocol':'dsh-native-binding/v2','metadata':self.binding.metadata,'pid':os.getpid()}

    def _reply(self, call, *args):
        try:
            return {'status':'completed','value':call(*args)}
        except SkillInferenceFailure as error:
            self.closed = True
            return {'status':'skill_failed','failure':error.record()}

    def binding_apply(self, packet: dict) -> dict:
        """Apply the parent's admitted plan through unchanged native validation."""
        if self.closed:
            raise RuntimeError('A stopped native worker cannot be rebound')
        return self._reply(self.binding.bind, packet['plan'], packet['observation'])

    def binding_observe(self, observation: dict) -> dict:
        """Forward the current observation without advancing physical simulation."""
        return self._reply(self.binding.observe, observation)

    def binding_actions(self) -> dict:
        """Return native action arrays through the existing numerical wire codec."""
        return self._reply(self.binding.actions)

    def binding_close(self) -> dict:
        """Close native skills; the parent must additionally await this process exit."""
        self.closed = True
        self.binding.close()
        return {'closed':True,'pid':os.getpid()}


async def serve(deployment: dict) -> None:
    """Serve one owned worker until the parent requests termination."""
    from PhysicalRSI_baselines.robodojo.skill_model import Model
    from client_server.ws.model_server import PolicyServer, PolicyServerConfig
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    model = WorkerModel(Model, deployment['model'])
    server = PolicyServer(model, PolicyServerConfig(host='127.0.0.1', port=0))
    try:
        await server.start()
        write_record(Path(deployment['ready_file']), {'url':server.url,'pid':os.getpid()})
        await stopped.wait()
    finally:
        try:
            await server.stop()
        finally:
            model.binding.close()


def main() -> None:
    """Start the private Python worker; Node processes still use DSH profiles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(json.loads(args.deployment.read_text())))


if __name__ == '__main__':
    main()
