"""Offline production RAG boundaries; temporary synthetic files, no formal writes."""
import json
import unittest
import uuid
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase
from .chat_runtime import ChatAgent
from .document_search import DocumentSearch, OneQuery, Deadline, document_fact, topic_supported
from .document_library import LibraryError
from .document_vectors import DocumentVectors
from .evidence_sources import public_sources, source_appendix, stored_sources
from .test_agent_loop import fact, found, Clock, plan
from . import test_document_library as fixtures
from .models import Conversation
from django.contrib.auth.models import User


def passage():
    return {'id':str(uuid.uuid4()), 'version_id':'a'*64, 'doc_id':'synthetic',
            'title':'合成资料', 'edition':'测试版', 'publisher':'测试', 'page':3,
            'printed_page':1, 'start':0, 'end':4, 'file_sha256':'b'*64,
            'text_sha256':'c'*64, 'source_url':'https://example.org/doc',
            'scope_note':'仅合成测试', 'text':'合成资料。'}


class ChatDocumentTests(SimpleTestCase):
    def setUp(self):
        self.graph = Mock(return_value=found(fact()))
        self.model = Mock()
        self.model.plan_query.return_value = plan()
        self.model.knowledge_qa.return_value = '资料 [E1]'
        self.docs = Mock()
        self.docs.preview.return_value = {'eligible':True}
        self.docs.search.return_value = [passage()]
        self.docs.last_report = {'status':'found', 'embedding_requests':1}
    def agent(self, q, history=None, **kw):
        return ChatAgent(self.graph, Mock(), lambda:self.model, query=q,
                         history=history or [], document_search=self.docs, **kw)
    def test_general_health_uses_documents_without_graph_or_query_model(self):
        q='如何看食品标签？'
        a=self.agent(q); r=a.retrieve(q,[])
        self.assertEqual(r['resolution'],'reviewed_documents')
        self.graph.assert_not_called(); self.model.plan_query.assert_not_called()
        self.assertEqual(r['facts'][0]['evidence_id'],'E1')
        self.assertEqual(a.answer(q,r,[]),'资料 [E1]')
        self.assertEqual(a.trace['counts']['documents'],1)
    def test_mixed_unified_ids(self):
        q='糖尿病有哪些症状和饮食知识？'
        a=self.agent(q); r=a.retrieve(q,[])
        self.assertEqual([f['evidence_id'] for f in r['facts']],['E1','E2'])
        self.assertEqual(r['facts'][1]['kind'],'document')
        self.assertIn('[E2]',r['context'])
        self.assertTrue(self.graph.called)
    def test_scope_skip_does_not_spend_embedding(self):
        self.docs.preview.return_value={'eligible':False,'reason':'scope'}
        a=self.agent('糖尿病症状'); r=a.retrieve('糖尿病症状',[])
        self.docs.search.assert_not_called()
        self.assertTrue(r['context'])
    def test_documents_failure_falls_back_to_graph_with_notice(self):
        self.docs.search.side_effect=RuntimeError('secret credential')
        a=self.agent('糖尿病症状'); r=a.retrieve('糖尿病症状',[])
        self.assertTrue(r['context']); self.assertIn('图谱',r['notice'])
        self.assertNotIn('secret',json.dumps(r))
    def test_empty_document_results_do_not_claim_joint_retrieval(self):
        self.docs.search.return_value=[]
        a=self.agent('糖尿病症状'); r=a.retrieve('糖尿病症状',[])
        self.assertTrue(r['context'])
        self.assertNotEqual(r['resolution'],'graph_and_reviewed_documents')
        self.assertTrue(all(f.get('kind')!='document' for f in r['facts']))
    def test_document_only_failure_never_generates_answer(self):
        self.docs.search.side_effect=LibraryError('service')
        a=self.agent('食品标签知识'); r=a.retrieve('食品标签知识',[])
        self.assertFalse(r['context']); self.model.knowledge_qa.assert_not_called()
    def test_preview_failure_has_notice_and_no_embedding(self):
        self.docs.preview.side_effect=RuntimeError('secret')
        a=self.agent('糖尿病症状'); r=a.retrieve('糖尿病症状',[])
        self.assertIn('图谱',r['notice']); self.docs.search.assert_not_called()
    def test_unknown_citation_rejected_once(self):
        self.model.knowledge_qa.return_value='资料 [E99]'
        a=self.agent('食品标签知识'); r=a.retrieve('食品标签知识',[])
        self.assertIsNone(a.answer('食品标签知识',r,[]))
        self.model.knowledge_qa.assert_called_once()
    def test_no_citation_rejected(self):
        self.model.knowledge_qa.return_value='没有引用'
        a=self.agent('食品标签知识'); r=a.retrieve('食品标签知识',[])
        self.assertIsNone(a.answer('食品标签知识',r,[]))
    def test_pronoun_resolved_before_document_query(self):
        a=self.agent('它的饮食呢？',[{'role':'user','content':'讨论糖尿病'}])
        a.retrieve('它的饮食呢？',[])
        self.assertIn('糖尿病',self.docs.search.call_args.args[0])
        self.assertNotIn('讨论',self.docs.search.call_args.args[0])
    def test_ambiguous_reference_no_document_call(self):
        a=self.agent('它呢？'); r=a.retrieve('它呢？',[])
        self.assertEqual(r['status'],'ambiguous'); self.docs.search.assert_not_called()
    def test_deadline_shared_with_document_tool(self):
        clock=Clock()
        def slow(*args,**kw): clock.now=91; return [passage()]
        self.docs.search.side_effect=slow
        a=self.agent('食品标签知识',clock=clock); r=a.retrieve('食品标签知识',[])
        self.assertEqual(r['agent']['stop_reason'],'deadline_exceeded'); self.assertFalse(r['context'])
    def test_context_budget_includes_question_state_and_documents(self):
        row=passage(); row['text']='大'*9000
        self.docs.search.return_value=[row]
        a=self.agent('食品标签知识'); r=a.retrieve('食品标签知识',[])
        self.assertFalse(r['context']); self.assertTrue(r['truncated'])


class SourceMetadataTests(SimpleTestCase):
    def test_document_and_graph_persist_without_path(self):
        facts=[dict(fact(),evidence_id='E1'),document_fact(passage(),'E2')]
        saved=source_appendix(facts)
        self.assertEqual(stored_sources('回答'+saved),public_sources(facts))
        self.assertNotIn('D:\\',saved)
        self.assertNotIn('file_sha256',json.dumps(public_sources(facts)))
    def test_bad_json_or_too_many_fail_closed(self):
        self.assertEqual(stored_sources('x'),[])
        self.assertEqual(stored_sources('\n\n资料出处（原始内容尚待医学审核）：\n<!-- medical-sources-v1:no-->'),[])
    def test_old_grouped_graph_appendix_remains_readable(self):
        rows=stored_sources('回答\n\n资料出处（原始内容尚待医学审核）：\n[E1, E2] 糖尿病的症状：medical.json 第17行')
        self.assertEqual([r['evidence_id'] for r in rows],['E1','E2'])
        self.assertEqual(rows[0]['line'],17)
    def test_more_than_thirty_graph_facts_do_not_hide_document_source(self):
        facts=[dict(fact(),evidence_id='E'+str(i)) for i in range(1,45)]
        facts.append(document_fact(passage(),'E45'))
        self.assertEqual(stored_sources('回答'+source_appendix(facts))[-1]['kind'],'document')
    def test_untrusted_link_dropped_and_path_rebuilt(self):
        row=passage(); row['source_url']='javascript:alert(1)'
        f=document_fact(row,'E1'); f['sources'][0]['file_url']='file:///etc/passwd'
        s=public_sources([f])[0]
        self.assertEqual(s['source_url'],'')
        self.assertEqual(s['file_url'],'/api/documents/'+'a'*64+'/file/')
    def test_invalid_version_or_page_cannot_link(self):
        f=document_fact(passage(),'E1'); f['sources'][0]['version_id']='../../secret'
        self.assertEqual(public_sources([f]),[])
        f=document_fact(passage(),'E1'); f['sources'][0]['page']=0
        self.assertEqual(public_sources([f]),[])


@override_settings(DOCUMENT_RAG_ENABLED=True)
class DocumentAPITests(APITestCase):
    def test_guest_response_sources_and_logged_in_history_sources(self):
        docs=Mock(); docs.preview.return_value={'eligible':True}; docs.search.return_value=[passage()]
        docs.last_report={'embedding_requests':1,'status':'found'}
        model=Mock(); model.knowledge_qa.return_value='资料 [E1]'
        with patch('api.views.DocumentSearch',return_value=docs),patch('api.views.DeepSeekAPI',return_value=model):
            guest=self.client.post('/api/ai/knowledge-qa/',{'query':'食品标签知识'},format='json')
            self.assertEqual(guest.status_code,200)
            self.assertEqual(guest.data['sources'][0]['page'],3)
            user=User.objects.create_user('rag-test','test@example.org','fixture')
            self.client.force_authenticate(user)
            c=Conversation.objects.create(user=user,title='新对话')
            response=self.client.post('/api/ai/knowledge-qa/',{'query':'食品标签知识','conversation_id':c.id},format='json')
            self.assertEqual(response.status_code,200)
            history=self.client.get(f'/api/conversations/{c.id}/').data['messages'][-1]
            self.assertEqual(history['sources'],response.data['sources'])
    def test_pdf_unknown_and_traversal_404(self):
        self.assertEqual(self.client.get('/api/documents/'+'0'*64+'/file/').status_code,404)
        self.assertEqual(self.client.get('/api/documents/invalid/file/').status_code,404)
    def test_pdf_known_hash_failure_not_served(self):
        from .document_search import SCOPES
        version=json.loads(SCOPES.read_text(encoding='utf-8'))['documents'][0]['version_id']
        library=Mock(); library.versions.return_value=[{'id':version}]
        library.verify.side_effect=LibraryError('corrupt')
        with patch('api.views.production_library',return_value=library):
            self.assertEqual(self.client.get('/api/documents/'+version+'/file/').status_code,404)


class DocumentToolTests(unittest.TestCase):
    # Reuse temporary synthetic PDF fixtures, but do not duplicate inherited test discovery.
    def setUp(self):
        fixtures.LibraryTests.setUp(self)
        self.version=fixtures.LibraryTests.imported(self); self.lib.activate(self.version)
        self.scopes=self.root/'scopes.json'
        self.scopes.write_text(json.dumps({'documents':[{'version_id':self.version,
            'diseases':['高血压'],'query_terms':['alpha']}]}),encoding='utf-8')
        self.search=DocumentSearch(self.lib,self.scopes)
        self.lexical=patch('api.document_search.lexical_search',return_value=[{'retrieval':{'score':5}}])
        self.lexical.start(); self.addCleanup(self.lexical.stop)
    def test_preview_scope_and_active_version(self):
        self.assertTrue(self.search.preview('alpha monitoring')['eligible'])
        self.assertFalse(self.search.preview('alpha monitoring',['肺癌'])['eligible'])
        self.assertFalse(self.search.preview('alpha 剂量')['eligible'])
        self.assertFalse(self.search.preview('alpha 食品标签 html 网页')['eligible'])
        self.lib.disable(self.meta['doc_id'])
        self.assertFalse(self.search.preview('alpha monitoring')['eligible'])
    def test_topic_anchor_rejects_generic_prevention_match(self):
        self.assertFalse(topic_supported({'version_id':'a','text':'青少年应加强运动，预防超重'}, {'a':['跌倒']}))
        self.assertTrue(topic_supported({'version_id':'a','text':'老年人预防跌\n倒'}, {'a':['跌倒']}))
    def test_unready_index_no_external_client(self):
        with patch('api.document_search.EmbeddingConfig.load',return_value=fixtures.Embedder.config),patch('api.document_search.EmbeddingClient') as client:
            with self.assertRaisesRegex(LibraryError,'not_ready'):
                self.search.search('alpha monitoring')
            client.assert_not_called()
    def test_one_query_budget_and_usage_unknown(self):
        client=Mock(); client.records=[{'total_tokens':None}]
        one=OneQuery(client,Deadline(2))
        with self.assertRaisesRegex(LibraryError,'usage_unknown'): one.embed(['test'],'query')
        with self.assertRaisesRegex(LibraryError,'query_limit'): one.embed(['test'],'query')
        client.embed.assert_called_once()
    def indexed_service(self, callback=None, corrupt=False):
        config=fixtures.Embedder.config
        qdrant=fixtures.FakeQdrant()
        store=DocumentVectors(self.lib,config.fingerprint(),qdrant)
        store.index_version(self.version,fixtures.Embedder())
        qdrant.corrupt=corrupt
        client=Mock(); client.config=config; client.records=[]
        def embed(texts,role,**kw):
            client.records.append({'total_tokens':10,'status':'ok'})
            if callback: callback()
            return [[1.0]+[0.0]*1023]
        client.embed.side_effect=embed
        row=self.lib.evidence(self.lib.chunks(self.version)[0]['id'],[self.version])
        row['retrieval']={'score':5}
        return config,qdrant,client,row
    def test_complete_tool_once_and_deduplicates_same_page(self):
        config,qdrant,client,row=self.indexed_service()
        with patch('api.document_search.EmbeddingConfig.load',return_value=config),patch('api.document_search.TimedQdrant',return_value=qdrant),patch('api.document_search.EmbeddingClient',return_value=client),patch('api.document_search.lexical_search',return_value=[row]):
            rows=self.search.search('alpha monitoring')
        self.assertEqual(len(rows),1); client.embed.assert_called_once()
        self.assertEqual(self.search.last_report['embedding_requests'],1)
        self.assertEqual(rows[0]['version_id'],self.version)
    def test_active_version_switch_during_query_is_not_returned(self):
        config,qdrant,client,row=self.indexed_service(lambda:self.lib.disable(self.meta['doc_id']))
        with patch('api.document_search.EmbeddingConfig.load',return_value=config),patch('api.document_search.TimedQdrant',return_value=qdrant),patch('api.document_search.EmbeddingClient',return_value=client),patch('api.document_search.lexical_search',return_value=[row]):
            with self.assertRaisesRegex(LibraryError,'active_versions_changed'):
                self.search.search('alpha monitoring')
        self.assertEqual(self.search.last_report['embedding_requests'],1)
    def test_corrupt_remote_index_blocks_spending(self):
        config,qdrant,client,row=self.indexed_service(corrupt=True)
        with patch('api.document_search.EmbeddingConfig.load',return_value=config),patch('api.document_search.TimedQdrant',return_value=qdrant),patch('api.document_search.EmbeddingClient',return_value=client),patch('api.document_search.lexical_search',return_value=[row]):
            with self.assertRaises(LibraryError): self.search.search('alpha monitoring')
        client.embed.assert_not_called()
