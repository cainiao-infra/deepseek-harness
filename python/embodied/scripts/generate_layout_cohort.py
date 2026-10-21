"""Generate development or preregistered validation layouts from PhysicalRSI's catalog."""
import argparse
import json
from pathlib import Path

from deepseek_harness_embodied.layout_cohort import generate_cohort

parser=argparse.ArgumentParser(description=__doc__)
for name in ('source','assets','output'):
    parser.add_argument('--'+name,type=Path,required=True)
parser.add_argument('--tasks',nargs='+',required=True)
parser.add_argument('--count',type=int,required=True)
parser.add_argument('--seed',type=int,required=True)
parser.add_argument('--split',choices=('development','validation'),required=True)
parser.add_argument('--comparison-sha256')
parser.add_argument('--workers',type=int,default=2)
parser.add_argument('--timeout',type=float,default=180)
args=parser.parse_args()
result=generate_cohort(**vars(args))
print(json.dumps({'state':result['state'],'assigned':len(result['entries']),
                  'generated':sum(r['state']=='generated' for r in result['entries']),'qualification':False}))
raise SystemExit(0 if result['state']=='generated' else 1)
