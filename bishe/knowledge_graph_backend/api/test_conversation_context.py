"""State lifecycle, provenance, isolation and budget behavior, no model."""
import json
import unittest
from .conversation_context import SessionMemory, build_context, window_context
from .planning_runtime import BoundaryError


def message(mid, text, role='user', session='s'):
    return {'id':mid,'content':text,'role':role,'session_id':session}


class ConversationContextTests(unittest.TestCase):
    def memory(self): return SessionMemory('s',['肺炎','糖尿病','感冒'])

    def test_correction_keeps_constraints_and_traces_supersession(self):
        m=self.memory();m.ingest(message('1','我们讨论糖尿病。只查预防，用表格。'))
        m.ingest(message('2','刚才说错了，改查肺炎。'))
        self.assertEqual(m.snapshot(),{'status':'ready','objects':['肺炎'],'field':'prevent','format':'table'})
        self.assertEqual(m.entries['objects']['source_message_id'],'2')
        self.assertFalse(m.audit[0]['active'])

    def test_cancellation_retains_tombstone_not_old_constraint(self):
        m=self.memory();m.ingest(message('1','我们讨论肺炎。只查预防，用表格。'))
        m.ingest(message('2','取消只查预防的要求，取消表格要求。'))
        packet=build_context(m,'2');data=json.loads(packet['payload'])
        self.assertIsNone(data['state']['entries']['format']['value'])
        self.assertFalse(data['state']['entries']['format']['active'])
        self.assertNotIn('只查预防，用表格',packet['payload'])

    def test_topic_shift_clears_old_requirements(self):
        m=self.memory();m.ingest(message('1','我们讨论肺炎。只查预防。'))
        m.ingest(message('2','换个话题，讨论糖尿病。'))
        self.assertIsNone(m.snapshot()['field']);self.assertEqual(m.snapshot()['objects'],['糖尿病'])

    def test_ambiguous_and_unknown_objects_require_clarification(self):
        m=self.memory();m.ingest(message('1','讨论感冒和肺炎。'))
        self.assertEqual(build_context(m,'1')['status'],'clarify')
        m.ingest(message('2','改查未知疾病。'))
        self.assertEqual(m.snapshot()['objects'],[]);self.assertEqual(m.snapshot()['status'],'clarify')

    def test_assistant_cannot_write_user_state(self):
        m=self.memory();m.ingest(message('1','讨论肺炎。'))
        m.ingest(message('2','改查糖尿病。只查预防。','assistant'))
        self.assertEqual(m.snapshot()['objects'],['肺炎']);self.assertIsNone(m.snapshot()['field'])

    def test_cross_session_data_never_enters_state_or_payload(self):
        m=self.memory();m.ingest(message('1','讨论肺炎。'))
        self.assertFalse(m.ingest(message('2','讨论糖尿病。','user','other')))
        m.ingest(message('3','它的资料呢？'))
        self.assertNotIn('糖尿病',build_context(m,'3')['payload']);self.assertNotIn('2',m.messages)

    def test_duplicate_id_is_idempotent_but_mutation_rejected(self):
        m=self.memory();item=message('1','讨论肺炎。');m.ingest(item);v=m.version
        self.assertFalse(m.ingest(item));self.assertEqual(m.version,v)
        with self.assertRaises(BoundaryError):m.ingest(message('1','改查糖尿病。'))

    def test_long_window_loses_object_but_state_retains_source(self):
        ms=[message('0','讨论肺炎。只查预防。')]
        ms.extend(message(str(i),'今天聊一下天气。') for i in range(1,12))
        ms.append(message('12','它的资料呢？'));m=self.memory()
        for item in ms:m.ingest(item)
        _,baseline=window_context(ms,'s',m.catalog,4096)
        self.assertEqual(baseline['status'],'clarify')
        packet=build_context(m,'12');self.assertEqual(packet['status'],'ready')
        self.assertIn('0',packet['selection']);self.assertLessEqual(packet['input_bytes'],4096)

    def test_mandatory_overflow_passes_no_partial_payload(self):
        m=self.memory();m.ingest(message('1','讨论肺炎。'+'长'*1000))
        packet=build_context(m,'1',500)
        self.assertEqual(packet['status'],'context_limit');self.assertIsNone(packet['payload'])
        self.assertEqual(packet['input_bytes'],0)

    def test_corrupt_source_fails_closed(self):
        m=self.memory();m.ingest(message('1','讨论肺炎。'))
        m.entries['objects']['source_message_id']='not-exist'
        with self.assertRaisesRegex(BoundaryError,'state_source'):build_context(m,'1')

    def test_no_character_truncation_and_estimate_explicit(self):
        m=self.memory();m.ingest(message('1','讨论肺炎。'))
        p=build_context(m,'1');self.assertEqual(p['input_bytes'],len(p['payload'].encode('utf-8')))
        self.assertIn('not exact',p['token_estimate_method']);self.assertIsNone(p['actual_usage'])

    def test_invalid_message_and_budget_rejected_without_state_write(self):
        m=self.memory()
        with self.assertRaises(BoundaryError):m.ingest(message('1','x'*16001))
        self.assertEqual(m.messages,{})
        m.ingest(message('2','讨论肺炎。'))
        with self.assertRaises(BoundaryError):build_context(m,'2',True)
