"""Render missing-detail notices locally, without generating medical examples.

This only bounds recognizable limitation sections. It is not a semantic verifier
for every medical claim in the main answer. No model call or automatic retry.
"""
import re

MISSING_NOTICE = '当前资料未覆盖的细节无法确认；请勿据此制定个人诊疗或用药方案，具体问题请咨询专业人员。'
LABEL = (r'需注意的缺失|资料缺失与说明|资料缺失说明|资料缺失|缺失与说明|缺失说明|'
         r'资料边界说明|信息不足之处|缺失信息|未覆盖内容|缺失与限制|缺失提示')
HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:' + LABEL + r')(?:[：:])?(?:\*\*)?\s*[：:]?.*$', re.M)
LIMIT_PARAGRAPH = re.compile(r'^(?:(?:当前|现有|本次|所给|已提供)(?:提供的|检索到的|收录的)?|提供的|检索到的|收录的)?'
                             r'(?:资料|摘录|证据)(?:中|里)?(?:未|没有|不足|无法|仅|只)')


def neutralize_missing_details(answer):
    policy = {'version':'neutral_missing_details_v1', 'replaced':False,
              'scope':'recognizable_limitation_sections_only_not_full_semantic_verification'}
    match = HEADING.search(answer)
    if match:
        # The answer prompt puts limitations last. Replace the entire tail so a
        # secondary subheading/list cannot leak unsupported examples back through.
        answer = answer[:match.start()].rstrip()
        policy['replaced'] = True
    paragraphs = re.split(r'\n\s*\n', answer.strip())
    kept = []
    for paragraph in paragraphs:
        plain = paragraph.strip().strip('*').strip()
        if LIMIT_PARAGRAPH.match(plain):
            policy['replaced'] = True
            # An answer beginning "current sources do not cover..." is an
            # insufficiency response; do not retain its unsourced advice tail.
            if not any(re.search(r'\bE\d+\b', previous) for previous in kept):
                return MISSING_NOTICE, dict(policy, rendering='neutral_notice_only')
            continue
        if paragraph.strip():
            kept.append(paragraph.strip())
    text = '\n\n'.join(kept)
    if policy['replaced']:
        text = (text + '\n\n' if text else '') + MISSING_NOTICE
    return text, dict(policy, rendering='answer_with_neutral_notice' if kept else 'neutral_notice_only')
