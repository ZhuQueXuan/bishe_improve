"""SDK wire contract and response boundary tests, with socket access forbidden by runner."""
import copy
import json
import unittest
from types import SimpleNamespace
import httpx2 as httpx
from .deepseek_planner import DeepSeekPlanner,MockPlannerTransport,build_messages,usage_record,parse_action
from .planning_runtime import BoundaryError,entity_id
from .test_planning_runtime import Tools,Clock,Verifier,TEMP,A
from .planning_runtime import PlanningHarness


def context():
    return {'task_id':'P01','question':A+'的资料','seed_id':entity_id(A),'selected_ids':[],
            'registry':[{'id':entity_id(A),'name':A,'origin':'explicit_question','evidence_ids':[]}],
            'requirements':{},'evidence_ids':[],'counts':{'reads':0,'decisions':1,'answers':0},'remaining_ms':90000}


def response(content=None,finish='stop',usage=True):
    body={'id':'mock','created':0,'object':'chat.completion','model':'deepseek-flash',
          'choices':[{'index':0,'finish_reason':finish,'message':{'role':'assistant','content':content or json.dumps({'kind':'read_relations','entity_id':entity_id(A),'relation':'acompany_with'})}}]}
    if usage:body['usage']={'prompt_tokens':40,'completion_tokens':20,'total_tokens':60,'prompt_cache_hit_tokens':10,'prompt_cache_miss_tokens':30}
    return body


class DeepSeekPlannerTests(unittest.TestCase):
    def transport(self,handler):
        transport=MockPlannerTransport(handler);self.addCleanup(transport.close);return transport
    def policy(self,body):return DeepSeekPlanner(self.transport(lambda request:httpx.Response(200,json=body)))

    def test_sdk_wire_configuration(self):
        captured=[]
        def handler(req):captured.append(json.loads(req.content));return httpx.Response(200,json=response())
        policy=DeepSeekPlanner(self.transport(handler));action=policy.decide(context(),2)
        self.assertEqual(action['kind'],'read_relations');body=captured[0]
        self.assertEqual(body['max_tokens'],300);self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(body['response_format'],{'type':'json_object'});self.assertFalse(body['stream'])
        self.assertEqual(policy.records[0]['timeout_seconds'],2)
        self.assertEqual(policy.records[0]['usage']['prompt_cache_hit_tokens'],10)

    def test_real_client_cannot_be_injected(self):
        with self.assertRaises(BoundaryError):DeepSeekPlanner(object())

    def test_unknown_fields_and_nested_raw_data_not_sent(self):
        data=context();data.update(oracle='secret-gold',history='formal-history',api_key='formal-key')
        data['registry'][0]['source']='raw-record';data['counts']['private']='private-count'
        text=json.dumps(build_messages(data),ensure_ascii=False)
        for forbidden in ['secret-gold','formal-history','formal-key','raw-record','private-count']:self.assertNotIn(forbidden,text)

    def test_input_size_fails_without_request(self):
        captured=[];policy=DeepSeekPlanner(self.transport(lambda req:captured.append(req)))
        data=context();data['question']='长'*4001
        with self.assertRaises(BoundaryError):policy.decide(data,15)
        self.assertEqual(captured,[])

    def test_utf8_byte_cap_includes_system_message(self):
        data=context();data['question']='长'*4000
        with self.assertRaises(BoundaryError):build_messages(data)

    def test_truncated_json_rejected_but_usage_retained(self):
        policy=self.policy(response('{',finish='length'))
        with self.assertRaises(BoundaryError):policy.decide(context(),15)
        self.assertEqual(policy.records[0]['usage']['total_tokens'],60)

    def test_missing_usage_remains_unknown(self):
        policy=self.policy(response(usage=False));policy.decide(context(),15)
        self.assertIsNone(policy.records[0]['usage'])

    def test_invalid_usage_not_counted_as_zero(self):
        self.assertIsNone(usage_record(SimpleNamespace(prompt_tokens=10,completion_tokens=2,total_tokens=999)))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(BoundaryError):parse_action('{"kind":"stop","kind":"read_relations"}')

    def test_nan_json_rejected(self):
        with self.assertRaises(BoundaryError):parse_action('{"kind":"stop","status":NaN,"reason":"x","evidence_ids":[]}')

    def test_markdown_code_fence_rejected(self):
        with self.assertRaises(BoundaryError):parse_action('```json\n{}\n```')

    def test_arbitrary_cypher_rejected(self):
        with self.assertRaises(BoundaryError):parse_action('{"kind":"cypher","query":"CREATE (n)"}')

    def test_extra_action_fields_rejected(self):
        with self.assertRaises(BoundaryError):parse_action('{"kind":"read_relations","entity_id":"x","relation":"acompany_with","write":true}')

    def test_error_has_no_sdk_retry_or_secret_text(self):
        captured=[]
        def handler(req):captured.append(req);return httpx.Response(429,json={'error':{'message':'secret-key internal error'}})
        policy=DeepSeekPlanner(self.transport(handler))
        with self.assertRaisesRegex(BoundaryError,'planner_transport_error'):policy.decide(context(),15)
        self.assertEqual(len(captured),1);self.assertNotIn('secret',str(policy.records))

    def test_adapter_max_four_requests(self):
        calls=[]
        def handler(req):calls.append(req);return httpx.Response(200,json=response())
        policy=DeepSeekPlanner(self.transport(handler))
        for _ in range(4):policy.decide(context(),15)
        with self.assertRaises(BoundaryError):policy.decide(context(),15)
        self.assertEqual(len(calls),4)

    def test_late_response_does_not_return_action(self):
        clock=Clock()
        def handler(req):clock.advance(3);return httpx.Response(200,json=response())
        policy=DeepSeekPlanner(self.transport(handler),clock)
        with self.assertRaisesRegex(BoundaryError,'planner_late_response'):policy.decide(context(),2)
        self.assertEqual(policy.records[0]['usage']['total_tokens'],60)

    def test_hidden_reasoning_not_saved(self):
        body=response();body['choices'][0]['message']['reasoning_content']='hidden-secret'
        policy=self.policy(body);policy.decide(context(),15)
        self.assertNotIn('hidden-secret',str(policy.records))

    def test_harness_rejects_undiscovered_model_entity(self):
        task={'id':'P01','question':A,'seed':{'id':entity_id(A),'name':A},
              'budget':{'read_calls':5,'planner_decisions':4,'answer_calls':1,'total_seconds':90}}
        bad=json.dumps({'kind':'read_fields','entity_ids':[entity_id('乙疾病')],'fields':['desc']})
        tools=Tools();controller=PlanningHarness(task,tools,Verifier(),'manifest',TEMP)
        result=controller.run(self.policy(response(bad)))
        self.assertEqual(result['status'],'blocked');self.assertEqual(tools.calls,[])
        self.assertEqual(result['counts']['decisions'],4)

    def test_harness_invalid_json_consumes_budget(self):
        task={'id':'P01','question':A,'seed':{'id':entity_id(A),'name':A},
              'budget':{'read_calls':5,'planner_decisions':4,'answer_calls':1,'total_seconds':90}}
        tools=Tools();controller=PlanningHarness(task,tools,Verifier(),'manifest',TEMP)
        result=controller.run(self.policy(response('{bad}')))
        self.assertEqual(result['counts']['decisions'],4);self.assertEqual(tools.calls,[])
