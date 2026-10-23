"""Test-owned typed inference fault before any physical command; never a deployment default."""
from pathlib import Path
import json
import os
import runpy
import sys

config=json.loads(Path(os.environ['DSH_ADMISSION_FAULT']).read_text())
arguments=sys.argv[1:]
if arguments[:2]!=['-m','deepseek_harness_embodied.native_worker']:
    os.execv(config['python'],[config['python'],*arguments])
from PhysicalRSI_baselines.robodojo import skill_model
from deepseek_harness_embodied.skill_failure import SkillExecutionFault
from deepseek_harness_embodied.bridge import write_record
Original=skill_model.Model
class InjectedModel(Original):
    """Raise one declared fault for the selected skill, leaving native cleanup unchanged."""
    def get_action_batch(self,*args,**kwargs):
        if self.sessions[0]['skill'].name==config['target']:
            marker=Path(config['marker'])
            if not marker.exists():
                write_record(marker,{'target':config['target'],'operation':'actions',
                    'robot_actions_before_fault':0,'injected':True})
                raise SkillExecutionFault('Controlled provider fault before first dispatch.',{
                    'reason':'incompatible','retry':'never','predicates':['scope_instruction'],'effects':'none'})
        return super().get_action_batch(*args,**kwargs)
skill_model.Model=InjectedModel
sys.argv=['deepseek_harness_embodied.native_worker',*arguments[2:]]
runpy.run_module('deepseek_harness_embodied.native_worker',run_name='__main__')
