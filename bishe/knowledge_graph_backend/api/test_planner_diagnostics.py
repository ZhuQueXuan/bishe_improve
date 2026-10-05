"""Safe diagnostic metadata and unchanged rejection/usage behavior."""
import json
import unittest
from decimal import Decimal
from uuid import uuid4
import httpx2 as httpx
from .deepseek_planner import response_structure, DeepSeekPlanner, MockPlannerTransport
from .authorized_planner import BatchLedger, AuthorizedTransport, AuthorizedPlanner
from .planning_runtime import BoundaryError, PlanningHarness, entity_id
from .test_deepseek_planner import context, response
from .test_planning_runtime import Tools, Verifier, TEMP, A


class PlannerDiagnosticsTests(unittest.TestCase):
    def test_missing_and_extra_keys_distinguished_without_echoing_text(self):
        content=json.dumps({'kind':'stop','status':'complete','reason':'secret-value',
                            'secret-key':'secret-value'})
        info=response_structure(content)
        self.assertEqual(info['missing_keys'],['evidence_ids'])
        self.assertEqual(info['unknown_key_count'],1)
        self.assertEqual(info['issues'],['missing_keys','extra_keys'])
        self.assertNotIn('secret',json.dumps(info))

    def test_wrong_types_and_array_item_types(self):
        info=response_structure(json.dumps({'kind':'read_fields','entity_ids':'secret-id','fields':[123]}))
        self.assertEqual(info['wrong_type_keys'],['entity_ids'])
        self.assertIn('array_item_type',info['issues'])
        self.assertNotIn('secret-id',json.dumps(info))

    def test_duplicate_and_nonfinite_json(self):
        info=response_structure('{"kind":"stop","kind":"stop","status":NaN,"reason":"x","evidence_ids":[]}')
        self.assertIn('duplicate_key',info['issues'])
        self.assertIn('nonfinite_json',info['issues'])

    def test_bad_json_root_and_unknown_kind(self):
        self.assertEqual(response_structure('{secret')['issues'],['invalid_json'])
        self.assertEqual(response_structure('[]')['issues'],['action_object'])
        info=response_structure('{"kind":"secret-kind"}')
        self.assertIn('action_kind',info['issues'])
        self.assertNotIn('secret-kind',json.dumps(info))

    def test_oversized_content_does_not_parse_or_leak(self):
        info=response_structure('secret'*1000)
        self.assertEqual(info,{'issues':['output_length'],'bytes':4097})
        self.assertEqual(response_structure(None)['issues'],['content_type'])

    def test_sdk_rejection_keeps_usage_and_safe_structure(self):
        content='{"kind":"read_relations","entity_id":"secret-value","relation":"acompany_with","secret-key":true}'
        transport=MockPlannerTransport(lambda req:httpx.Response(200,json=response(content)))
        self.addCleanup(transport.close);policy=DeepSeekPlanner(transport)
        with self.assertRaisesRegex(BoundaryError,'planner_action_schema'):policy.decide(context(),15)
        record=policy.records[0]
        self.assertEqual(record['usage']['total_tokens'],60)
        self.assertEqual(record['response_structure']['unknown_key_count'],1)
        self.assertNotIn('secret',json.dumps(record))

    def test_invalid_action_still_charges_budget_and_ledger(self):
        content='{"kind":"read_relations","entity_id":"x","relation":"acompany_with","extra":true}'
        mock=MockPlannerTransport(lambda req:httpx.Response(200,json=response(content)))
        self.addCleanup(mock.close)
        ledger=BatchLedger(TEMP/('diagnostic_test_'+uuid4().hex+'.json'),TEMP)
        transport=AuthorizedTransport(mock._client,ledger);transport.task_id='P01'
        policy=AuthorizedPlanner(transport);policy.local_only=True
        task={'id':'P01','question':A,'seed':{'id':entity_id(A),'name':A},
              'budget':{'read_calls':5,'planner_decisions':4,'answer_calls':1,'total_seconds':90}}
        tools=Tools();controller=PlanningHarness(task,tools,Verifier(),'manifest',TEMP)
        result=controller.run(policy)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['counts']['decisions'],4)
        self.assertEqual(tools.calls,[])
        self.assertEqual(len(ledger.data['calls']),4)
        self.assertTrue(all(x['status']=='settled' and Decimal(x['estimated_rmb'])>0 for x in ledger.data['calls']))
        self.assertEqual(controller.context()['last_action_result']['reason_code'],'planner_action_schema')

    def test_normal_response_still_parses_without_exposing_reason(self):
        body=response(json.dumps({'kind':'stop','status':'blocked','reason':'private-text','evidence_ids':[]}))
        mock=MockPlannerTransport(lambda req:httpx.Response(200,json=body));self.addCleanup(mock.close)
        policy=DeepSeekPlanner(mock);action=policy.decide(context(),15)
        self.assertEqual(action['reason'],'private-text')
        self.assertEqual(policy.records[0]['response_structure']['issues'],[])
        self.assertNotIn('private-text',json.dumps(policy.records))
