"""Failure-driven tests for public feedback and checkpoint continuation."""
import copy,json,unittest,uuid
from .planning_runtime import PlanningHarness,BoundaryError
from .planning_policies import RulePolicy,MockDynamicPolicy
from .deepseek_planner import build_messages
from .test_planning_runtime import Tools,Verifier,Clock,TEMP,A,entity_id,Actions


class PlanningFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.task={'id':'P01','question':A,'seed':{'id':entity_id(A),'name':A},
                   'budget':{'read_calls':5,'planner_decisions':4,'answer_calls':1,'total_seconds':90}}
        self.tools=Tools();self.controller=self.make(self.tools)
    def make(self,tools):return PlanningHarness(self.task,tools,Verifier(),'manifest',TEMP,self.clock,self.clock)
    def fields_loaded(self,controller=None):
        c=controller or self.controller;c.step(MockDynamicPolicy());c.step(MockDynamicPolicy());return c
    def bad_stop(self):
        return Actions({'kind':'stop','status':'complete','reason':'fields only','evidence_ids':['E3','E4','E5','E6']})

    def test_stop_contract_includes_discovery_and_field_evidence(self):
        c=self.fields_loaded();state=c.context()
        self.assertEqual(state['required_stop_evidence_ids'],['E1','E2','E3','E4','E5','E6'])
        self.assertTrue(state['stop_ready']);self.assertEqual(state['stop_status'],'complete')
        self.assertEqual(state['readable_fields'],[])

    def test_rejection_reaches_next_sdk_request(self):
        c=self.fields_loaded();c.step(self.bad_stop())
        state=json.loads(build_messages(c.context())[1]['content'])['task_state']
        self.assertEqual(state['last_action_result'],{'status':'rejected','reason_code':'incomplete_citations'})
        self.assertEqual(state['required_stop_evidence_ids'],['E1','E2','E3','E4','E5','E6'])
        self.assertEqual(c.state['phase'],'active');self.assertEqual(len(self.tools.calls),3)

    def test_missing_is_handled_not_readable(self):
        c=self.fields_loaded(self.make(Tools('missing')))
        state=c.context()
        self.assertEqual(state['stop_status'],'partial')
        self.assertEqual(len(state['handled_missing_fields']),1)
        self.assertFalse(state['handled_missing_fields'][0]['retry_allowed'])
        self.assertEqual(state['readable_fields'],[])

    def test_retry_missing_rejected_and_visible_without_tool_call(self):
        tools=Tools('missing');c=self.fields_loaded(self.make(tools));eid=c.state['selected_ids'][0]
        c.step(Actions({'kind':'read_fields','entity_ids':[eid],'fields':['prevent']}))
        state=json.loads(build_messages(c.context())[1]['content'])['task_state']
        self.assertEqual(state['last_action_result']['reason_code'],'resolved_field_repeat')
        self.assertEqual(len(tools.calls),3)

    def test_feedback_survives_checkpoint_and_does_not_reset_counts(self):
        c=self.fields_loaded();c.step(self.bad_stop())
        before=c.context();path=TEMP/('feedback_test_'+uuid.uuid4().hex+'.json');c.checkpoint(path)
        c=PlanningHarness.restore(path,self.task,self.tools,Verifier(),'manifest',TEMP,self.clock,self.clock)
        self.assertEqual(c.context()['last_action_result'],before['last_action_result'])
        self.assertEqual(c.context()['required_stop_evidence_ids'],before['required_stop_evidence_ids'])
        c.step(MockDynamicPolicy())
        self.assertEqual(c.state['phase'],'complete');self.assertEqual(c.state['counts']['decisions'],4)
        self.assertEqual(c.state['counts']['reads'],3)

    def test_unknown_error_content_not_sent(self):
        c=self.controller;c.state['trace'].append({'event':'rejected','reason':'secret-key raw exception'})
        body=build_messages(c.context())[1]['content']
        self.assertNotIn('secret-key',body)
        self.assertEqual(c.context()['last_action_result']['reason_code'],'action_rejected')

    def test_adapter_recomputes_required_ids_instead_of_trusting_extra_input(self):
        c=self.fields_loaded();state=c.context();state['required_stop_evidence_ids']=['E999']
        output=json.loads(build_messages(state)[1]['content'])['task_state']
        self.assertNotIn('E999',output['required_stop_evidence_ids'])
        self.assertEqual(len(output['required_stop_evidence_ids']),6)

    def test_accepted_read_replaces_stale_rejection(self):
        c=self.controller;c.step(Actions({'kind':'cypher','query':'no'}))
        self.assertEqual(c.context()['last_action_result']['status'],'rejected')
        c.step(MockDynamicPolicy())
        self.assertEqual(c.context()['last_action_result'],{'status':'found'})

    def test_partial_read_keeps_only_remaining_fields_readable(self):
        c=self.controller;c.step(MockDynamicPolicy());first=c.state['selected_ids'][0]
        c.step(Actions({'kind':'read_fields','entity_ids':[first],'fields':['desc']}))
        pending={row['entity_id']:row['fields'] for row in c.context()['readable_fields']}
        self.assertEqual(pending[first],['prevent'])
        self.assertFalse(c.context()['stop_ready'])

    def test_no_automatic_correction_of_model_action(self):
        c=self.fields_loaded();c.run(self.bad_stop())
        self.assertEqual(c.state['phase'],'blocked')
        self.assertEqual(c.state['stop_reason'],'decision_budget')
        self.assertEqual(len(self.tools.calls),3)
