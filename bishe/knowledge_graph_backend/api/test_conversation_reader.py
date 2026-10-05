"""Live-reader contract/cost boundaries tested through local HTTP mocks."""
import json
import unittest
from uuid import uuid4
import httpx2 as httpx
from .conversation_reader import ConversationReader,ContextBatchLedger,parse_reading,SYSTEM,CALL_KEYS
from .authorized_planner import AuthorizedTransport
from .deepseek_planner import MockPlannerTransport
from .planning_runtime import BoundaryError
from .test_planning_runtime import TEMP


def reply(content,usage=True,finish='stop'):
    data={'id':'mock','created':0,'object':'chat.completion','model':'deepseek-flash',
          'choices':[{'index':0,'finish_reason':finish,'message':{'role':'assistant','content':content}}]}
    if usage:data['usage']={'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}
    return data


GOOD='{"status":"ready","objects":["肺炎"],"field":"prevent","format":null}'


class ConversationReaderTests(unittest.TestCase):
    def ledger(self):return ContextBatchLedger(TEMP/('context_reader_test_'+uuid4().hex+'.json'),TEMP)
    def mock(self,handler):
        t=MockPlannerTransport(handler);self.addCleanup(t.close);return t

    def test_extra_keys_duplicates_and_nonfinite_rejected(self):
        for content in [GOOD[:-1]+',"secret-key":"secret-value"}',
                        GOOD[:-1]+',"status":"ready"}',GOOD.replace('null','NaN')]:
            with self.assertRaises(BoundaryError):parse_reading(content,['肺炎'])

    def test_unknown_objects_types_and_status_consistency(self):
        for content in [GOOD.replace('肺炎','unknown'),GOOD.replace('ready','clarify'),
                        GOOD.replace('["肺炎"]','"肺炎"'),GOOD.replace('prevent','other')]:
            with self.assertRaises(BoundaryError):parse_reading(content,['肺炎'])

    def test_total_input_budget_includes_common_system_before_call(self):
        calls=[];reader=ConversationReader(self.mock(lambda req:calls.append(req)),['肺炎'])
        with self.assertRaisesRegex(BoundaryError,'context_request_budget'):
            reader.read({'payload':'{}'},len(SYSTEM.encode('utf-8')))
        self.assertEqual(calls,[]);self.assertEqual(reader.records,[])

    def test_each_pair_once_and_batch_maximum(self):
        ledger=self.ledger()
        for key in CALL_KEYS:
            ledger.settle(ledger.reserve(key),{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50})
        self.assertEqual(len(ledger.data['calls']),16)
        for key in [CALL_KEYS[0],'P01','C09:state_context']:
            with self.assertRaises(BoundaryError):ledger.reserve(key)

    def test_unknown_usage_halts_future_requests(self):
        ledger=self.ledger();ledger.settle(ledger.reserve(CALL_KEYS[0]),None)
        self.assertEqual(ledger.data['halt_reason'],'usage_unknown')
        with self.assertRaises(BoundaryError):ledger.reserve(CALL_KEYS[1])

    def test_rejected_response_still_settles_usage_without_raw_text(self):
        content=GOOD[:-1]+',"secret-key":"secret-value"}'
        mock=self.mock(lambda req:httpx.Response(200,json=reply(content)))
        ledger=self.ledger();t=AuthorizedTransport(mock._client,ledger);t.task_id=CALL_KEYS[0]
        reader=ConversationReader(t,['肺炎'])
        with self.assertRaisesRegex(BoundaryError,'context_response_schema'):reader.read({'payload':'{}'},4096)
        self.assertEqual(ledger.data['calls'][0]['status'],'settled')
        self.assertEqual(reader.records[0]['usage']['total_tokens'],50)
        self.assertNotIn('secret',str(reader.records))

    def test_transport_failure_has_no_retry_or_secret_record(self):
        calls=[]
        def handler(req):
            calls.append(req);return httpx.Response(429,json={'error':{'message':'secret-key'}})
        mock=self.mock(handler);ledger=self.ledger();t=AuthorizedTransport(mock._client,ledger);t.task_id=CALL_KEYS[0]
        reader=ConversationReader(t,['肺炎'])
        with self.assertRaises(BoundaryError):reader.read({'payload':'{}'},4096)
        self.assertEqual(len(calls),1);self.assertTrue(ledger.data['halted'])
        self.assertNotIn('secret',str(reader.records))

    def test_sdk_wire_and_success_have_no_hidden_reasoning(self):
        captured=[]
        def handler(req):
            captured.append(json.loads(req.content));data=reply(GOOD)
            data['choices'][0]['message']['reasoning_content']='hidden-secret'
            return httpx.Response(200,json=data)
        reader=ConversationReader(self.mock(handler),['肺炎'])
        self.assertEqual(reader.read({'payload':'{}'},4096)['objects'],['肺炎'])
        body=captured[0];self.assertEqual(body['max_tokens'],300)
        self.assertFalse(body['stream']);self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertNotIn('hidden-secret',str(reader.records))
