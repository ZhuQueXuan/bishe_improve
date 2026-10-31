"""Offline language contract examples, never model-quality scores."""
import json,unittest
from .test_natural_memory_policy import Harness
from .test_natural_memory_retrieval import packet,intent,answer
from .natural_memory_retrieval import validate_answer
from .natural_memory_contract import NaturalMemoryContractError

class PresentationTests(unittest.TestCase):
    def setUp(self):
        h=Harness();s=h.source('请回顾已保存的本人情况。');self.job=packet(h,s,intent(s,'memory_review'))
    def test_natural_qualifiers_are_accepted_unchanged(self):
        examples=['你曾明确说自己有示例病。','你曾说已经排除示例病。','你曾说可能有示例病，还没确定。',
            '你当时不确定自己是否有示例病。','你当时觉得自己可能没有示例病，但还拿不准。','你曾说前年有过示例病。']
        for value in examples:self.assertEqual(validate_answer(answer(self.job,value),self.job)['answer'],value)
    def test_metadata_dump_rejected_not_silently_rewritten(self):
        for value in ['clinical_confirmation=false','user_confirmed=true','event_state: reported','肺炎（asserted）','肯定（affirmed）']:
            with self.assertRaisesRegex(NaturalMemoryContractError,'answer_internal_metadata'):validate_answer(answer(self.job,value),self.job)
    def test_prompt_asks_for_fluent_qualified_prose(self):
        prompt=self.job['messages'][0]['content']
        for value in ['自然、简洁的中文','可能没有','是否有','前年','不能为了语气温和','不在answer正文展示']:self.assertIn(value,prompt)
    def test_old_version_request_reference_is_stale(self):
        value=json.loads(answer(self.job));value['request_ref']='answer_old-version'
        with self.assertRaisesRegex(NaturalMemoryContractError,'stale_answer'):validate_answer(json.dumps(value),self.job)
