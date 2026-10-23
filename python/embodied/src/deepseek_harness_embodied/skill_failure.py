"""Confirmed inference failures, distinct from unknown process or wire outcomes."""
from __future__ import annotations


def validate_handling(value: dict) -> dict:
    """Validate provider-declared recovery semantics at the worker JSON interface."""
    if (not isinstance(value, dict) or set(value) != {'reason','retry','predicates','effects'}
            or not isinstance(value['reason'],str) or not isinstance(value['retry'],str)
            or not isinstance(value['effects'],str)
            or value['reason'] not in {'incompatible','missing_observation','dependency_unavailable','transient','unknown'}
            or value['retry'] not in {'never','after_predicate_change','bounded_transient'}
            or value['effects'] not in {'none','partial','unknown'}
            or not isinstance(value['predicates'], list)
            or any(not isinstance(v,str) or not v for v in value['predicates'])
            or len(set(value['predicates'])) != len(value['predicates'])
            or value['retry']=='after_predicate_change' and not value['predicates']
            or value['reason']=='dependency_unavailable' and value['retry']!='never'
            or value['retry']=='bounded_transient' and value['reason']!='transient'):
        raise ValueError('Invalid provider failure handling')
    return {**value,'predicates':list(value['predicates'])}


class SkillExecutionFault(RuntimeError):
    """Provider-declared inference fault; cleanup and robot stopping are still required."""
    def __init__(self, detail: str, handling: dict):
        self.handling = validate_handling(handling)
        super().__init__(detail)


class SkillInferenceFailure(RuntimeError):
    """Inference failed and the owner confirmed cleanup; no robot stop is implied.

    A caller must still stop dispatch, discard cached actions, confirm physical
    quiescence and acquire a fresh observation before considering a replacement.
    Diagnostic text stays outside model-visible execution-event facts.
    """
    kind = 'skill_inference_failed'

    def __init__(self, operation: str, error_type: str, detail: str, handling: dict | None = None) -> None:
        self.declared_handling = handling is not None
        self.handling = validate_handling(handling if handling is not None else {
            'reason':'unknown','retry':'never','predicates':[],'effects':'unknown'})
        self.operation, self.error_type, self.detail = operation, error_type, detail
        super().__init__(f'Native {operation} inference failed ({error_type}): {detail}')

    def record(self) -> dict:
        """Return diagnostic fields for the private worker protocol and evidence."""
        return {'operation':self.operation,'error_type':self.error_type,'detail':self.detail,
                **({'handling':self.handling} if self.declared_handling else {})}


def decode_result(result: object, operation: str):
    """Validate a private worker response; only an explicit fault permits cleanup review."""
    if not isinstance(result, dict):
        raise ValueError('Native worker response must be an object')
    if result.get('status')=='completed' and set(result)=={'status','value'}:
        return result['value']
    if result.get('status')!='skill_failed' or set(result)!={'status','failure'}:
        raise ValueError('Unknown native worker response')
    failure=result['failure']
    if (not isinstance(failure,dict) or set(failure) not in ({'operation','error_type','detail'}, {'operation','error_type','detail','handling'})
            or failure['operation']!=operation or not isinstance(failure['error_type'],str)
            or not failure['error_type'] or not isinstance(failure['detail'],str)):
        raise ValueError('Native worker fault fields are invalid')
    return SkillInferenceFailure(**failure)
