"""Confirmed inference failures, distinct from unknown process or wire outcomes."""
from __future__ import annotations


class SkillInferenceFailure(RuntimeError):
    """Inference failed and the owner confirmed cleanup; no robot stop is implied.

    A caller must still stop dispatch, discard cached actions, confirm physical
    quiescence and acquire a fresh observation before considering a replacement.
    Diagnostic text stays outside model-visible execution-event facts.
    """
    kind = 'skill_inference_failed'

    def __init__(self, operation: str, error_type: str, detail: str) -> None:
        self.operation, self.error_type, self.detail = operation, error_type, detail
        super().__init__(f'Native {operation} inference failed ({error_type}): {detail}')

    def record(self) -> dict:
        """Return diagnostic fields for the private worker protocol and evidence."""
        return {'operation':self.operation,'error_type':self.error_type,'detail':self.detail}


def decode_result(result: object, operation: str):
    """Validate a private worker response; only an explicit fault permits cleanup review."""
    if not isinstance(result, dict):
        raise ValueError('Native worker response must be an object')
    if result.get('status')=='completed' and set(result)=={'status','value'}:
        return result['value']
    if result.get('status')!='skill_failed' or set(result)!={'status','failure'}:
        raise ValueError('Unknown native worker response')
    failure=result['failure']
    if (not isinstance(failure,dict) or set(failure)!={'operation','error_type','detail'}
            or failure['operation']!=operation or not isinstance(failure['error_type'],str)
            or not failure['error_type'] or not isinstance(failure['detail'],str)):
        raise ValueError('Native worker fault fields are invalid')
    return SkillInferenceFailure(**failure)
