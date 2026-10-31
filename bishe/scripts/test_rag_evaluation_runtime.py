"""Offline source isolation and spend controls; no Docker or live SDK."""
from pathlib import Path
import json
import socket
import unittest
from unittest.mock import Mock, patch
import uuid
from types import SimpleNamespace as NS
from rag_evaluation_runtime import make_agent, Ledger, GuardedSDK, GuardedEmbeddingTransport, ExperimentStop

ROOT=Path(__file__).resolve().parents[2]


def graph_result():
    return {'status':'found','facts':[{'disease':'糖尿病','field':'desc','label':'描述','value':'合成图谱资料',
        'medical_review_status':'not_verified','sources':[{'file':'bishe/data/medical.json','file_sha256':'a'*64,
        'line':1,'record_id':'1','disease':'糖尿病','field':'desc'}]}], 'diseases':['糖尿病']}


def docrow():
    return {'id':str(uuid.uuid4()),'version_id':'a'*64,'doc_id':'fixture','title':'测试资料','edition':'合成版',
            'publisher':'fixture','page':1,'printed_page':1,'start':0,'end':4,'text':'合成文档资料',
            'file_sha256':'b'*64,'text_sha256':'c'*64,'source_url':'https://example.org/doc','scope_note':'合成测试'}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.block=patch.object(socket.socket,'connect',side_effect=AssertionError('network blocked'))
        self.block.start(); self.addCleanup(self.block.stop)
        self.graph=Mock(return_value=graph_result()); self.phrase=Mock()
        self.model=Mock()
        self.model.plan_query.return_value=json.dumps({'medical':True,'diseases':['糖尿病'],'fields':['desc'],'keywords':[]})
        self.model.knowledge_qa.return_value='资料 [E1]'
        self.docs=Mock()
        self.docs.preview.return_value={'eligible':True,'version_ids':['a'*64]}
        self.docs.search.return_value=[docrow()]
        self.docs.last_report={'status':'found','embedding_requests':1}
    def agent(self, mode, query='糖尿病是什么？', history=()):
        return make_agent(mode,self.graph,self.phrase,lambda:self.model,self.docs,query=query,history=list(history))
    def test_graph_cannot_touch_document_tool(self):
        agent=self.agent('G'); result=agent.retrieve('糖尿病是什么？',[])
        self.assertTrue(result['context']); self.docs.preview.assert_not_called(); self.docs.search.assert_not_called()
        self.assertTrue(all(f.get('kind')!='document' for f in result['facts']))
    def test_documents_cannot_touch_graph_or_query_model(self):
        agent=self.agent('D'); result=agent.retrieve('糖尿病是什么？',[])
        self.assertEqual(result['facts'][0]['kind'],'document')
        self.graph.assert_not_called(); self.phrase.assert_not_called(); self.model.plan_query.assert_not_called()
        self.assertEqual(self.docs.search.call_args.kwargs['diseases'],['糖尿病'])
        self.assertEqual(agent.answer('糖尿病是什么？',result,[]),'资料 [E1]')
    def test_document_scope_miss_no_fallback_or_model(self):
        self.docs.preview.return_value={'eligible':False,'reason':'scope'}
        agent=self.agent('D'); result=agent.retrieve('糖尿病是什么？',[])
        self.assertFalse(result['context']); self.graph.assert_not_called(); self.docs.search.assert_not_called()
    def test_document_error_no_fallback(self):
        self.docs.search.side_effect=RuntimeError('secret')
        agent=self.agent('D'); result=agent.retrieve('糖尿病是什么？',[])
        self.assertFalse(result['context']); self.graph.assert_not_called()
        self.assertNotIn('secret',json.dumps(result))
    def test_joint_uses_production_mixed_path(self):
        agent=self.agent('J'); result=agent.retrieve('糖尿病是什么？',[])
        self.assertEqual([f.get('kind','graph') for f in result['facts']],['graph','document'])
    def test_identical_session_resolution_for_every_mode(self):
        h=[{'id':'u1','role':'user','content':'讨论糖尿病'}]
        for mode in ['G','D','J']:
            a=self.agent(mode,'它是什么？',h); r=a.retrieve('它是什么？',h)
            self.assertEqual(a.answer_query,'糖尿病：它是什么？')
            self.assertEqual(r['session']['state']['objects'],['糖尿病'])
    def test_ambiguity_never_spends(self):
        h=[{'id':'u1','role':'user','content':'我想了解糖尿病、胃炎这两种疾病。'}]
        for mode in ['G','D','J']:
            a=self.agent(mode,'它是什么？',h); r=a.retrieve('它是什么？',h)
            self.assertEqual(r['status'],'ambiguous')
        self.graph.assert_not_called(); self.docs.preview.assert_not_called(); self.model.plan_query.assert_not_called()
    def test_fixed_pilot_explicit_plural_now_requires_clarification(self):
        h=[{'id':'u1','role':'user','content':'我想了解糖尿病和高血压这两种疾病。'}]
        for mode in ['G','D','J']:
            a=self.agent(mode,'它需要注意什么？',h)
            self.assertEqual(a.memory.snapshot()['objects'],['糖尿病','高血压'])
            self.assertEqual(a.retrieve('它需要注意什么？',h)['status'],'ambiguous')
        self.graph.assert_not_called(); self.docs.preview.assert_not_called(); self.model.plan_query.assert_not_called()
    def test_unknown_citation_no_retry(self):
        self.model.knowledge_qa.return_value='资料 [E999]'
        a=self.agent('D'); r=a.retrieve('糖尿病是什么？',[])
        self.assertIsNone(a.answer('糖尿病是什么？',r,[])); self.model.knowledge_qa.assert_called_once()


class BudgetTests(unittest.TestCase):
    def ledger(self,allowed=True,budget='1'):
        path=ROOT/'temp/rag_evaluation_v1/offline'/uuid.uuid4().hex/'ledger.json'
        ledger=Ledger(path,allow_external=allowed,budget=budget)
        ledger.current='Q01-J'
        return ledger
    def test_default_no_external_opt_in(self):
        ledger=self.ledger(False)
        with self.assertRaises(ExperimentStop): ledger.reserve('answer',100,4096)
        self.assertFalse(ledger.data['calls'])
    def test_per_run_single_call(self):
        ledger=self.ledger(); ledger.reserve('answer',100,4096)
        with self.assertRaisesRegex(ExperimentStop,'call_limit'): ledger.reserve('answer',100,4096)
        self.assertEqual(len(ledger.data['calls']),1)
    def test_global_understanding_cap(self):
        ledger=self.ledger()
        for i in range(12):
            ledger.current=f'fixture{i}-J'; row=ledger.reserve('understand',100,600)
            ledger.settle(row,{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20})
        ledger.current='fixture12-J'
        with self.assertRaisesRegex(ExperimentStop,'call_limit'): ledger.reserve('understand',100,600)
    def test_unknown_usage_blocks_subsequent_request(self):
        ledger=self.ledger(); row=ledger.reserve('embedding',10)
        with self.assertRaisesRegex(ExperimentStop,'usage_unknown'): ledger.settle(row,None)
        with self.assertRaises(ExperimentStop): ledger.reserve('answer',100,4096)
    def test_cost_precheck_before_request(self):
        ledger=self.ledger(budget='0.000001')
        with self.assertRaisesRegex(ExperimentStop,'cost_reservation_limit'): ledger.reserve('answer',100,4096)
        self.assertFalse(ledger.data['calls'])
    def test_mode_call_isolation(self):
        for mode,kind in [('G','embedding'),('D','understand')]:
            ledger=self.ledger(); ledger.current='Q01-'+mode
            with self.assertRaisesRegex(ExperimentStop,'source_mode'): ledger.reserve(kind,100,600)
    def test_sdk_failure_once_and_stops(self):
        ledger=self.ledger(); raw=Mock(); raw.with_options.return_value.chat.completions.create.side_effect=OSError('secret')
        client=GuardedSDK(raw,ledger)
        args={'model':'deepseek-flash','stream':False,'max_tokens':4096,'timeout':10,
              'extra_body':{'thinking':{'type':'disabled'}},'messages':[{'role':'user','content':'fixture'}]}
        with self.assertRaisesRegex(ExperimentStop,'transport_failure'): client.create(**args)
        with self.assertRaises(ExperimentStop): client.create(**args)
        raw.with_options.return_value.chat.completions.create.assert_called_once()
        self.assertNotIn('secret',ledger.path.read_text(encoding='utf-8'))
    def test_sdk_usage_settles(self):
        ledger=self.ledger(); raw=Mock()
        raw.with_options.return_value.chat.completions.create.return_value=NS(
            usage=NS(prompt_tokens=10,completion_tokens=10,total_tokens=20),
            choices=[NS(finish_reason='stop',message=NS(content='fixture'))])
        GuardedSDK(raw,ledger).create(model='deepseek-flash',stream=False,max_tokens=600,timeout=10,
            extra_body={'thinking':{'type':'disabled'}},messages=[{'role':'user','content':'fixture'}])
        self.assertEqual(ledger.data['calls'][0]['status'],'settled')
    def test_embedding_cannot_reembed_documents(self):
        ledger=self.ledger(); raw=Mock()
        with self.assertRaisesRegex(ExperimentStop,'embedding_scope'):
            GuardedEmbeddingTransport(raw,ledger).post(None,{'input':{'texts':['fixture']},'parameters':{'text_type':'document'}},10)
        raw.post.assert_not_called()
    def test_closed_batch_cannot_resume_or_overwrite(self):
        ledger=self.ledger(); ledger.close('complete')
        with self.assertRaises(ExperimentStop): ledger.reserve('answer',100,4096)
        with self.assertRaises(FileExistsError): Ledger(ledger.path,allow_external=True)


if __name__=='__main__': unittest.main()
