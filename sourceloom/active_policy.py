"""Versioned correction policies for active composition checkpoints."""

import re


EVIDENCE_GAP_POLICY_REFRESH_VERSION = 'active-plan-evidence-gap-policy-refresh-v1'
_EVIDENCE_GAP_ERROR_PREFIX = '规划没有保存本轮声明的 EvidenceGap ID：'
_FAILED_STAGE_PREFIX = '当前阶段结构修正后仍不成立：'
_EVIDENCE_GAP_CORRECTION_INSTRUCTION = (
    'For every missing ID named in this error, add exactly one result.evidence_gaps '
    'entry with that exact ID. Use declared_evidence_gaps to preserve its original meaning '
    'and source_id, bind it to the matching current SourceObligation, and retain any saved '
    'binding or resolution. Mark retrieval failures unresolved; never invent evidence or '
    'mark a gap resolved without an exact quote from an opened resource. If an older '
    'checkpoint has only the ID, use the matching action_history entry to preserve its '
    'bounded subject without broadening it')


def missing_evidence_gap_policy(error):
    """Parse the precise exhausted-plan failure and return its new correction policy."""
    raw_error = str(error or '')
    if raw_error.startswith(_FAILED_STAGE_PREFIX):
        raw_error = raw_error[len(_FAILED_STAGE_PREFIX):]
    if not raw_error.startswith(_EVIDENCE_GAP_ERROR_PREFIX):
        return None
    tail = raw_error[len(_EVIDENCE_GAP_ERROR_PREFIX):]
    gap_ids = [value.strip() for value in tail.split('、')]
    if not gap_ids or any(not value or re.search(r'\s', value) for value in gap_ids):
        return None
    return dict(error=raw_error, gap_ids=gap_ids,
                instruction=_EVIDENCE_GAP_CORRECTION_INSTRUCTION)
