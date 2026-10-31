"""Offline release wiring checks; no real account DB, model or retrieval network."""
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from . import natural_memory_intake as intake,natural_memory_answering as answering
from .natural_memory_models import NaturalMemoryDispatchBudget,NaturalMemoryBackgroundTask,NaturalMemoryLedger
from .natural_memory_release import ReleaseProvider,professional,counter
from .models import Conversation
from .test_natural_memory_background import ChatProvider

@override_settings(ROOT_URLCONF='api.urls',NATURAL_MEMORY_ISOLATED_ENABLED=True,NATURAL_MEMORY_ISOLATED_DATABASE='memory',
    NATURAL_MEMORY_BACKGROUND_ENABLED=True,NATURAL_MEMORY_DISPATCH_CANDIDATE_ENABLED=True,NATURAL_MEMORY_LIVE_DISPATCH_ENABLED=True)
class ReleaseWiringTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='release-fictional')
    def test_construction_and_import_create_no_budget(self):
        ReleaseProvider();self.assertFalse(NaturalMemoryDispatchBudget.objects.exists())
    def test_successive_operations_have_independent_owner_bound_envelopes(self):
        with patch('api.natural_memory_release.verify_release',return_value=True), patch('api.natural_memory_release.MANIFEST') as manifest:
            manifest.read_bytes.return_value=b'fictional-authorized-release'
            first=ReleaseProvider().for_user(self.user);second=ReleaseProvider().for_user(self.user)
        self.assertNotEqual(first.batch_key,second.batch_key)
        self.assertEqual(NaturalMemoryDispatchBudget.objects.count(),2)
        self.assertTrue(all(b.owner_ids==[self.user.pk] and b.attempts==0 for b in NaturalMemoryDispatchBudget.objects.all()))
    @override_settings(NATURAL_MEMORY_PREFERENCE_AUTO_RELEASED=True)
    def test_release_gate_does_not_enable_user_collection_or_auto_save(self):
        intake.initialize_account(self.user);row=NaturalMemoryLedger.objects.get(user=self.user)
        self.assertTrue(row.policy['preference_auto_gate_passed'])
        self.assertEqual(row.policy['collect_types'],[])
        self.assertFalse(row.policy['preference_auto_enabled']);self.assertFalse(row.permissions['cloud_extraction'])
    def test_unconfigured_account_does_not_allocate_model_envelope(self):
        from .natural_memory_chat import respond
        from types import SimpleNamespace
        request=SimpleNamespace(user=self.user,data={'query':'虚构问题'})
        with patch('api.natural_memory_dispatch.provider_for_user',side_effect=AssertionError('not_before_optin')):
            self.assertIsNone(respond(request))
        self.assertFalse(NaturalMemoryDispatchBudget.objects.exists())
    def test_no_professional_evidence_replies_without_blocking_for_confirmation(self):
        intake.initialize_account(self.user);row=NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user,expected_epoch=row.epoch,expected_policy_version=1,collect_types=['self_disease_report'],
            use_enabled=True,cloud_extraction=True,cloud_verification=True)
        row.refresh_from_db();answering.configure_grants(self.user,expected_version=0,expected_epoch=row.epoch,cloud_routing=True,cloud_answer_personal_context=True)
        conv=Conversation.objects.create(user=self.user);provider=ChatProvider();provider.professional=lambda *args:[]
        client=APIClient();client.force_authenticate(self.user)
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            result=client.post('/ai/knowledge-qa/',{'query':'虚构缺资料问题','conversation_id':conv.pk,'client_message_id':'one'},format='json')
        self.assertEqual(result.status_code,200);self.assertIn('没有找到足够',result.data['answer'])
        self.assertFalse(result.data['memory']['natural_memory']['confirmation_required_to_continue_chat'])
        self.assertEqual(provider.calls,['route']);self.assertEqual(NaturalMemoryBackgroundTask.objects.filter(status='queued').count(),1)
    def test_memory_review_never_opens_document_search_or_embeddings(self):
        with patch('api.document_search.DocumentSearch',side_effect=AssertionError('not_for_memory_review')):
            self.assertEqual(professional('虚构记忆回顾','memory_review'),[])
    def test_current_message_without_answerable_question_still_does_not_wait_for_memory(self):
        import json
        intake.initialize_account(self.user);row=NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user,expected_epoch=row.epoch,expected_policy_version=1,collect_types=['presentation_preference'],
            use_enabled=True,cloud_extraction=True,cloud_verification=True)
        row.refresh_from_db();answering.configure_grants(self.user,expected_version=0,expected_epoch=row.epoch,cloud_routing=True,cloud_answer_personal_context=True)
        provider=ChatProvider()
        def route(job):
            payload=json.loads(job['messages'][-1]['content'])
            return json.dumps({'contract':'natural-memory-query-intent-v1','request_ref':job['request_ref'],'purpose':'unclear','subject':'unknown',
                'target':{'mode':'none','names':[]},'time':{'scope':'current','raw':None},'current_preferences':[],'evidence':payload['sources'][-1]['text']})
        provider.route=route;provider.professional=lambda *args:[]
        conv=Conversation.objects.create(user=self.user);client=APIClient();client.force_authenticate(self.user)
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            result=client.post('/ai/knowledge-qa/',{'query':'以后回答简短一些。','conversation_id':conv.pk,'client_message_id':'statement'},format='json')
        self.assertEqual(result.status_code,200)
        self.assertNotIn('确认',result.data['answer']);self.assertEqual(NaturalMemoryBackgroundTask.objects.filter(status='queued').count(),1)
    def test_packaged_tokenizer_counts_without_temp_dependency(self):
        self.assertGreater(counter()([{'role':'system','content':'测试规则'},{'role':'user','content':'虚构输入'}]),0)
