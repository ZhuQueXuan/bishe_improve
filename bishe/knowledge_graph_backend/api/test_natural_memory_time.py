"""Time prompt/contract regressions; original H02 is now a SEEN fixture only."""
import copy,importlib.util,json
from pathlib import Path
import unittest
from . import natural_memory_extraction as current
from .test_natural_memory_contract import snapshot,envelope,disease
from .test_natural_memory_extraction import args,extraction,review

ROOT=Path(__file__).resolve().parents[3]
OLD=ROOT/'bishe/data/natural_memory_v1/r8_reserved_single_live_20261030'

class NaturalMemoryTimeTests(unittest.TestCase):
    def packet(self,body='此刻我仍有湿疹。',*,raw='此刻',scope='current_report',polarity='affirmed',certainty='asserted',name='湿疹'):
        source=snapshot(body);candidate=disease(source['messages'][-1],value=name,certainty=certainty)
        candidate['content']['polarity']=polarity;candidate['time']={'raw':raw,'temporal_scope':scope}
        return source,candidate

    def result(self,source,items):
        raw=extraction(source,items);return current.preview(source,extraction_response=raw,**args(source))

    def test_all_prompt_examples_validate_and_cover_three_time_scopes(self):
        source=snapshot();job=current.prepare_extraction(source,**args(source))
        examples=json.loads(job['messages'][0]['content'].split('输出合同与示例JSON：',1)[1])['format_examples']
        scopes=set()
        for example in examples:
            s=snapshot(example['input_text']);response=copy.deepcopy(example['output'])
            response['request_ref']=current.prepare_extraction(s,**args(s))['request_ref']
            result=current.preview(s,extraction_response=json.dumps(response,ensure_ascii=False),**args(s))
            self.assertNotEqual(result['status'],'processing_failed',result)
            self.assertFalse(result['writable']);self.assertFalse(result['writes_performed'])
            scopes.update(x['time']['temporal_scope'] for x in response['items'] if x['category']=='self_disease_report')
        self.assertEqual(scopes,{'current_report','past','unspecified'})
        self.assertEqual(job['prompt_version'],'natural-memory-extraction-prompt-v5')

    def test_current_null_still_fails_no_local_time_fill(self):
        s,c=self.packet(raw=None);r=self.result(s,[c])
        self.assertEqual(r['reason'],'time_basis_required');self.assertEqual(r['supported'],[])

    def test_past_null_still_fails(self):
        s,c=self.packet('上个月我有过湿疹。',raw=None,scope='past')
        self.assertEqual(self.result(s,[c])['reason'],'time_basis_required')

    def test_no_time_stays_unspecified_without_inferring_from_timestamp(self):
        s,c=self.packet('我有湿疹。',raw=None,scope='unspecified')
        raw=extraction(s,[c]);rv=review(s,raw)
        r=current.preview(s,extraction_response=raw,review_response=rv,**args(s))
        self.assertEqual(r['supported'][0]['grounded']['candidate']['time'],{'raw':None,'temporal_scope':'unspecified'})

    def test_complete_current_reaches_review_with_original_quote(self):
        s,c=self.packet();r=self.result(s,[c]);self.assertEqual(r['status'],'awaiting_review')
        sent=json.loads(r['request']['messages'][-1]['content'])['items'][0]['candidate']
        self.assertEqual(sent['time'],c['time']);self.assertEqual(sent['evidence'],c['evidence'])

    def test_complete_past_reaches_review(self):
        s,c=self.packet('上个月我有过湿疹。',raw='上个月',scope='past')
        self.assertEqual(self.result(s,[c])['status'],'awaiting_review')

    def test_normalized_date_not_in_evidence_rejected(self):
        s,c=self.packet(raw='2026-10-31');self.assertEqual(self.result(s,[c])['reason'],'time_not_in_user_evidence')

    def test_negated_possible_and_time_preserved_after_scripted_review(self):
        s,c=self.packet('此刻我怀疑自己可能没有湿疹。',polarity='negated',certainty='possible')
        raw=extraction(s,[c]);r=current.preview(s,extraction_response=raw,review_response=review(s,raw),**args(s))
        self.assertEqual(r['supported'][0]['grounded']['candidate'],c);self.assertFalse(r['clinical_confirmation'])

    def test_context_time_requires_its_owned_user_quote(self):
        prior=envelope('上个月我被告知有湿疹。',id='db:2');s=snapshot('说的是我本人，上个月那一次。',context=[prior])
        c=disease(s['messages'][-1],value='湿疹');c['time']={'raw':'上个月','temporal_scope':'past'}
        c['evidence']=[{'source_ref':'context_1','text':prior['content']},{'source_ref':'current','text':s['messages'][-1]['content']}]
        self.assertEqual(self.result(s,[c])['status'],'awaiting_review')
        c['evidence'][0]['source_ref']='unknown'
        self.assertEqual(self.result(s,[c])['reason'],'evidence_not_owned_user_source')

    def test_one_bad_time_rejects_entire_multi_item_response(self):
        s,c=self.packet('此刻我有湿疹，也有哮喘。');second=copy.deepcopy(c)
        second['content']['value_raw']='哮喘';second['time']['raw']=None
        r=self.result(s,[c,second]);self.assertEqual(r['reason'],'time_basis_required');self.assertEqual(r['supported'],[])

    def test_frozen_original_h02_failure_and_new_prompt_staleness_are_both_preserved(self):
        observed=json.loads((OLD/'audit/independent-analysis.json').read_text(encoding='utf-8'))['observations'][1]
        source=observed['source'];events=[json.loads(s) for s in (OLD/'real-run/case-02/attempts.jsonl').read_text(encoding='utf-8').splitlines()]
        raw=next(e['result']['content'] for e in events if e['event']=='response')
        spec=importlib.util.spec_from_file_location('api._frozen_time_v3',OLD/'code/backend/natural_memory_extraction.py')
        legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
        self.assertEqual(legacy.preview(source,extraction_response=raw,**args(source))['reason'],'time_basis_required')
        self.assertEqual(current.preview(source,extraction_response=raw,**args(source))['reason'],'stale_extraction_response')

    def test_seen_h02_synthetic_complete_response_reaches_review_without_changing_original(self):
        observed=json.loads((OLD/'audit/independent-analysis.json').read_text(encoding='utf-8'))['observations'][1]
        source=observed['source'];before=copy.deepcopy(observed['raw_extraction']);candidate=copy.deepcopy(before['items'][0])
        candidate['time']={'raw':'现在','temporal_scope':'current_report'}
        self.assertEqual(self.result(source,[candidate])['status'],'awaiting_review')
        self.assertIsNone(observed['raw_extraction']['items'][0]['time']['raw'])
