"""Scripted review-alignment checks; no new model understanding is claimed."""
import copy,importlib.util,json
from pathlib import Path
import unittest
from . import natural_memory_extraction as current
from .natural_memory_contract import NaturalMemoryContractError,digest
from . import natural_memory_policy as policy
from .test_natural_memory_contract import snapshot,envelope,disease
from .test_natural_memory_extraction import args,extraction,review
from .test_natural_memory_policy import Harness
ROOT=Path(__file__).resolve().parents[3]
OLD=ROOT/'bishe/data/natural_memory_v1/r8_uncertainty_remaining_live_20261031'
class ReviewAlignmentTests(unittest.TestCase):
 def packet(self,body='我可能没有湿疹，不过说不准。',polarity='negated',certainty='possible'):
  source=snapshot(body);candidate=disease(source['messages'][-1],value='湿疹',polarity=polarity,certainty=certainty)
  return source,candidate,extraction(source,[candidate])
 def response(self,source,ex,verdict='unclear',reason='wrong_certainty'):
  obj=json.loads(review(source,ex,verdict=verdict));obj['items'][0]['reason']=reason
  return json.dumps(obj,ensure_ascii=False)
 def result(self,s,ex,rv):return current.preview(s,extraction_response=ex,review_response=rv,**args(s))
 def test_current_extraction_v5_review_v6(self):
  s,c,ex=self.packet();job=current.prepare_review(s,ex,**args(s))
  self.assertEqual(current.prepare_extraction(s,**args(s))['prompt_version'],'natural-memory-extraction-prompt-v5')
  self.assertEqual(job['prompt_version'],'natural-memory-review-prompt-v6')
 def test_all_new_full_examples_roundtrip_without_writes(self):
  s,c,ex=self.packet();job=current.prepare_review(s,ex,**args(s))
  rules=json.loads(job['messages'][0]['content'].split('输出合同与示例JSON：',1)[1])
  self.assertEqual(set(rules['certainty_meaning']['uncertain_values']),{'unknown','possible'})
  self.assertEqual(len(rules['supported_examples']),3)
  for example in rules['supported_examples']:
   source=snapshot(example['input_text']);raw=extraction(source,[example['candidate']]);rv=copy.deepcopy(example['output'])
   rv['request_ref']=current.prepare_review(source,raw,**args(source))['request_ref']
   result=self.result(source,raw,json.dumps(rv,ensure_ascii=False))
   self.assertEqual(result['status'],'proposed');self.assertEqual(result['supported'][0]['grounded']['candidate'],example['candidate'])
   self.assertFalse(result['writable']);self.assertFalse(result['clinical_confirmation'])
 def test_supported_uncertain_negative_and_unknown_remain_unchanged(self):
  for body,polarity,certainty in [('我可能没有湿疹。','negated','possible'),('不知道自己有没有湿疹。','unknown','possible'),('不知道自己有没有湿疹。','unknown','unknown')]:
   s,c,ex=self.packet(body,polarity,certainty);result=self.result(s,ex,review(s,ex))
   self.assertEqual(result['supported'][0]['grounded']['candidate'],c)
 def test_supported_asserted_statement_remains_asserted(self):
  s,c,ex=self.packet('我有湿疹。','affirmed','asserted');result=self.result(s,ex,review(s,ex))
  self.assertEqual(result['supported'][0]['grounded']['candidate']['content'],c['content'])
 def test_unclear_with_known_negative_reason_is_pending_never_supported(self):
  s,c,ex=self.packet();rv=self.response(s,ex);before=rv;result=self.result(s,ex,rv)
  self.assertEqual(result['status'],'needs_confirmation');self.assertEqual(result['supported'],[])
  unclear=result['unclear'][0];self.assertEqual(unclear['grounded']['candidate'],c)
  self.assertEqual(unclear['review']['reason'],'wrong_certainty');self.assertFalse(unclear['review']['reason_pair_consistent'])
  self.assertEqual(result['review_response_hash'],digest(before));self.assertEqual(rv,before)
  self.assertFalse(result['writable']);self.assertFalse(result['writes_performed'])
 def test_unclear_with_other_known_reason_also_cannot_authorize(self):
  s,c,ex=self.packet();result=self.result(s,ex,self.response(s,ex,reason='wrong_subject'))
  self.assertEqual(result['status'],'needs_confirmation');self.assertEqual(result['supported'],[])
 def test_unknown_reason_still_rejected(self):
  s,c,ex=self.packet();self.assertEqual(self.result(s,ex,self.response(s,ex,reason='arbitrary_reason'))['reason'],'review_reason')
 def test_supported_with_negative_reason_still_rejected(self):
  s,c,ex=self.packet();self.assertEqual(self.result(s,ex,self.response(s,ex,verdict='supported'))['reason'],'review_reason')
 def test_contradicted_with_positive_reason_still_rejected(self):
  s,c,ex=self.packet();self.assertEqual(self.result(s,ex,self.response(s,ex,verdict='contradicted',reason='meaning_supported'))['reason'],'review_reason')
 def test_unclear_reason_tolerance_does_not_allow_forged_quote(self):
  s,c,ex=self.packet();obj=json.loads(self.response(s,ex));obj['items'][0]['evidence'][0]['text']='伪造原话'
  self.assertEqual(self.result(s,ex,json.dumps(obj,ensure_ascii=False))['reason'],'review_quote_missing')
 def test_unclear_reason_tolerance_does_not_allow_assistant_evidence(self):
  s=snapshot(context=[envelope('我有银屑病。',id='db:2',role='assistant')]);ex=extraction(s)
  obj=json.loads(self.response(s,ex));obj['items'][0]['evidence']=[{'source_ref':'context_1','text':'我有银屑病。'}]
  self.assertEqual(self.result(s,ex,json.dumps(obj,ensure_ascii=False))['reason'],'review_evidence_not_user')
 def test_item_reference_still_bound(self):
  s,c,ex=self.packet();obj=json.loads(self.response(s,ex));obj['items'][0]['item_ref']='item_99'
  self.assertEqual(self.result(s,ex,json.dumps(obj,ensure_ascii=False))['reason'],'review_item_ref')
 def test_review_permission_still_required(self):
  s,c,_=self.packet();options=args(s,verification=False);ex=extraction(s,[c],options=options)
  result=current.preview(s,extraction_response=ex,review_response='{}',**options)
  self.assertEqual(result['status'],'blocked');self.assertEqual(result['reason'],'permission_verification_disabled')
 def test_unclear_is_preserved_in_R3_pending_and_not_eligible(self):
  h=Harness();s=h.source('我可能没有湿疹，不过说不准。');c=disease(s['messages'][-1],value='湿疹',polarity='negated',certainty='possible')
  ex=extraction(s,[c]);rv=self.response(s,ex)
  result=policy.propose(h.state,s,permissions=args(s)['permissions'],request_key='unclear-review',extraction_response=ex,review_response=rv,**h.options())
  h.state=result['state'];self.assertEqual(result['status'],'needs_clarification');self.assertEqual(h.eligible(),[])
  self.assertTrue(h.state['pending']['unresolved']);self.assertEqual(h.state['entries'],{})
  fact=h.state['pending']['clarification_facts'][0];self.assertEqual(fact['content'],c['content'])
  self.assertEqual(h.state['pending']['source'],s)
 def test_unclear_pending_cannot_be_activated_by_simple_accept(self):
  h=Harness();s=h.source();ex=extraction(s);rv=self.response(s,ex)
  staged=policy.propose(h.state,s,permissions=args(s)['permissions'],request_key='unclear-review',extraction_response=ex,review_response=rv,**h.options())
  h.state=staged['state']
  with self.assertRaisesRegex(NaturalMemoryContractError,'clarification_required'):h.accept()
  self.assertEqual(h.eligible(),[])
 def test_confirmed_health_preserves_both_certainty_bands(self):
  for body,polarity,certainty in [('我有湿疹。','affirmed','asserted'),('我可能没有湿疹。','negated','possible'),('不知道自己有没有湿疹。','unknown','unknown')]:
   h=Harness();s=h.source(body);c=disease(s['messages'][-1],value='湿疹',polarity=polarity,certainty=certainty)
   h.stage(s,[c]);self.assertEqual(h.eligible(),[]);h.accept();entry=h.eligible()[0]
   self.assertEqual(entry['fact']['content'],c['content']);self.assertFalse(entry['clinical_confirmation'])
 def test_old_H12_failure_unchanged_and_current_rejects_stale_review(self):
  o=json.loads((OLD/'audit/independent-analysis.json').read_text(encoding='utf-8'))['observations'][5]
  events=[json.loads(s) for s in (OLD/'real-run/case-06/attempts.jsonl').read_text(encoding='utf-8').splitlines()]
  replies={e['stage']:e['result']['content'] for e in events if e['event']=='response'}
  spec=importlib.util.spec_from_file_location('api._frozen_review_v4',OLD/'code/backend/natural_memory_extraction.py')
  legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
  self.assertEqual(legacy.preview(o['source'],extraction_response=replies['extraction'],review_response=replies['review'],**args(o['source']))['reason'],'review_reason')
  self.assertEqual(self.result(o['source'],replies['extraction'],replies['review'])['reason'],'stale_extraction_response')
 def test_wrong_certainty_in_either_direction_can_be_rejected_without_rewriting(self):
  for original,observed in [('possible','asserted'),('asserted','possible')]:
   body='我有湿疹。' if original=='asserted' else '我可能有湿疹。'
   s,c,ex=self.packet(body,'affirmed',observed);rv=self.response(s,ex,verdict='contradicted')
   result=self.result(s,ex,rv);self.assertEqual(result['status'],'rejected')
   self.assertEqual(result['contradicted'][0]['grounded']['candidate']['content']['certainty'],observed)
   self.assertFalse(result['writable'])
