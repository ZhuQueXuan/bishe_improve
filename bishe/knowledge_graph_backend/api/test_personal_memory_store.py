"""P02 actual isolated ORM transactions, provenance, idempotency and races."""
import copy
import hashlib
import json
import threading
import uuid
from unittest.mock import patch
from django.contrib.auth.models import User, AnonymousUser
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from .models import (Conversation, ChatMessage, MemorySettings, MemorySourceExclusion, PersonalMemoryPolicy,
                     PersonalMemoryEntry, PersonalMemoryRevision, PersonalMemoryReceipt, PersonalMemoryIndexTask)
from .personal_memory_store import (PersonalMemoryStoreError, read_policy, set_policy, commit_candidate,
                                    read_current, read_history)
from .personal_memory_contract import TYPE_RULES


class StoreFixtures:
    def setUp(self):
        super().setUp()
        self.user=User.objects.create(username='p02-synthetic')
        self.other=User.objects.create(username='p02-other')
        self.conv=Conversation.objects.create(user=self.user)
        self.serial=0;self.hashes={};self.conv_versions={}
        self.policy=set_policy(self.user,expected_version=0,collect_types=list(TYPE_RULES),history_conversation_ids=[],use_enabled=True)

    def candidate(self,text='我有高血压。',*,user=None,conv=None,category='self_disease_report',value='高血压',
                  assertion='reported',details=None,mode='self_report',**changes):
        user=user or self.user;conv=conv or self.conv
        msg=ChatMessage.objects.create(conversation=conv,role='user',content=text)
        self.hashes[msg.pk]=hashlib.sha256(text.encode()).hexdigest();self.conv_versions[msg.pk]=conv.memory_version
        kind=TYPE_RULES[category][0];event=kind=='event'
        r={'id':uuid.uuid4().hex,'owner':f'user:{user.pk}','session':f'conversation:{conv.pk}','subject':'self',
           'kind':kind,'attribute':category+':'+value if event else 'length','value':value,
           'scope':'long_term','scope_ref':None,'storage_state':'active','event_state':'current' if event else 'not_applicable',
           'event_id':'event-one' if event else None,'recorded_at':msg.created_at.isoformat(),
           'event_time':{'raw':None,'start':None,'end':None},'revision':1,
           'source':{'message_id':f'db:{msg.pk}','start':0,'end':len(text),'text':text,'mode':mode}}
        r.update(changes)
        return {'category':category,'assertion':assertion if event else 'not_applicable','record':r,'details':details or {}},msg

    def commit(self,c,msg,**kw):
        self.serial+=1
        defaults={'request_key':f'p02-{self.serial}','expected_policy_version':self.policy['version'],
                  'source_conversation_version':self.conv_versions[msg.pk],'expected_source_hash':self.hashes[msg.pk]}
        defaults.update(kw)
        return commit_candidate(self.user,c,**defaults)

    def save(self):
        c,msg=self.candidate();r=self.commit(c,msg);return c,msg,r

    def correction(self):
        return self.candidate('更正，我没有高血压。',assertion='denied',event_state='denied',mode='explicit_request')

    def counts(self):
        return tuple(m.objects.count() for m in [PersonalMemoryEntry,PersonalMemoryRevision,PersonalMemoryReceipt,PersonalMemoryIndexTask])


class PersonalMemoryStoreTests(StoreFixtures, TestCase):
    def test_reads_do_not_create_policy_or_data(self):
        before=self.counts()
        self.assertEqual(read_policy(self.other),{'version':0,'collect_types':[],'history_conversation_ids':[],'use_enabled':False})
        self.assertIsNone(read_current(self.other,uuid.uuid4().hex));self.assertEqual(read_history(self.other,uuid.uuid4().hex),[])
        self.assertFalse(PersonalMemoryPolicy.objects.filter(user=self.other).exists());self.assertEqual(before,self.counts())

    def test_legacy_automatic_flag_never_authorizes_new_collection(self):
        MemorySettings.objects.create(user=self.other,auto_enabled=True,health_collect_enabled=True)
        conv=Conversation.objects.create(user=self.other);c,msg=self.candidate(user=self.other,conv=conv)
        with self.assertRaisesRegex(PersonalMemoryStoreError,'stale_or_missing_policy'):
            commit_candidate(self.other,c,request_key='old-flag',expected_policy_version=1,
                             source_conversation_version=0,expected_source_hash=self.hashes[msg.pk])
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_policy_exact_version_owned_history_and_strict_fields(self):
        foreign=Conversation.objects.create(user=self.other)
        for args,error in [({'expected_version':0},'stale_policy'),
                           ({'history_conversation_ids':[foreign.pk]},'history_not_owned'),
                           ({'collect_types':['unknown']},'invalid_types'),
                           ({'history_conversation_ids':[True]},'invalid_history_selection')]:
            params={'expected_version':self.policy['version'],'collect_types':[],'history_conversation_ids':[],'use_enabled':False};params.update(args)
            with self.assertRaisesRegex(PersonalMemoryStoreError,error):set_policy(self.user,**params)
        self.assertEqual(read_policy(self.user),self.policy)

    def test_initial_save_is_source_bound_and_usable_before_indexing(self):
        c,msg,r=self.save();self.assertEqual(self.counts(),(1,1,1,1));self.assertEqual(r['status'],'committed')
        current=read_current(self.user,r['memory_key'])
        self.assertEqual(current['index_status'],'pending');self.assertFalse(current['user_confirmed'])
        self.assertEqual(current['candidate'],c)
        revision=PersonalMemoryRevision.objects.get();job=PersonalMemoryIndexTask.objects.get()
        self.assertEqual(revision.source_time,msg.created_at);self.assertEqual(revision.source_hash,self.hashes[msg.pk])
        self.assertEqual(job.revision_id,revision.pk);self.assertEqual(job.policy_version,self.policy['version'])
        self.assertFalse(r['clinical_confirmation'])

    def test_duplicate_request_and_conflicting_body(self):
        c,msg=self.candidate();r=self.commit(c,msg,request_key='stable');before=self.counts()
        replay=self.commit(c,msg,request_key='stable')
        self.assertEqual(replay['status'],'replayed');self.assertFalse(replay['writes_performed']);self.assertEqual(before,self.counts())
        changed=copy.deepcopy(c);changed['record']['id']=uuid.uuid4().hex
        with self.assertRaisesRegex(PersonalMemoryStoreError,'request_key_conflict'):self.commit(changed,msg,request_key='stable')
        self.assertEqual(before,self.counts())

    def test_policy_revision_change_blocks_pending_candidate(self):
        c,msg=self.candidate()
        set_policy(self.user,expected_version=1,collect_types=[],history_conversation_ids=[],use_enabled=False)
        with self.assertRaisesRegex(PersonalMemoryStoreError,'stale_or_missing_policy'):self.commit(c,msg)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_cross_account_and_assistant_source_are_rejected(self):
        foreign=Conversation.objects.create(user=self.other)
        c,msg=self.candidate(user=self.other,conv=foreign)
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_not_owned'):self.commit(c,msg)
        c,msg=self.candidate();ChatMessage.objects.filter(pk=msg.pk).update(role='assistant')
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_unavailable'):self.commit(c,msg)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_full_message_hash_catches_changes_outside_selected_span(self):
        c,msg=self.candidate('我有高血压。补充情况以后再说。')
        c['record']['source'].update(end=7,text='我有高血压。')
        # Python Unicode span uses actual length, not guessed offsets.
        c['record']['source']['end']=len(c['record']['source']['text'])
        ChatMessage.objects.filter(pk=msg.pk).update(content='我有高血压。其他语境已变化。')
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_changed'):self.commit(c,msg)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_bad_span_time_and_conversation_revision(self):
        c,msg=self.candidate();c['record']['source']['text']='错误的原话'
        with self.assertRaisesRegex(PersonalMemoryStoreError,'candidate_source_invalid'):self.commit(c,msg)
        c,msg=self.candidate();c['record']['recorded_at']='2023-01-01T00:00:00+08:00'
        with self.assertRaisesRegex(PersonalMemoryStoreError,'candidate_source_invalid'):self.commit(c,msg)
        c,msg=self.candidate();Conversation.objects.filter(pk=self.conv.pk).update(memory_version=1)
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_conversation_changed'):self.commit(c,msg)

    def test_source_reset_or_exclusion_blocks_writes_and_reads(self):
        c,msg,r=self.save();MemorySourceExclusion.objects.create(user=self.user,message_id=msg.pk)
        self.assertIsNone(read_current(self.user,r['memory_key']))
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_excluded_or_reset'):self.commit(c,msg)
        c,msg=self.candidate();Conversation.objects.filter(pk=self.conv.pk).update(memory_after_id=msg.pk)
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_excluded_or_reset'):self.commit(c,msg)

    def test_source_edit_or_delete_invalidates_current_without_read_writes(self):
        c,msg,r=self.save();before=self.counts();ChatMessage.objects.filter(pk=msg.pk).update(content='我说的情况已经改变。')
        self.assertIsNone(read_current(self.user,r['memory_key']));self.assertEqual(before,self.counts())
        msg.delete()
        self.assertIsNone(read_current(self.user,r['memory_key']));self.assertEqual(PersonalMemoryRevision.objects.count(),1)

    def test_revision_update_preserves_old_and_stales_old_job(self):
        c,msg,r=self.save();new,source=self.correction()
        result=self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='correction')
        self.assertEqual(result['version'],2);self.assertEqual(self.counts(),(1,2,2,2))
        self.assertEqual(list(PersonalMemoryRevision.objects.order_by('version').values_list('status',flat=True)),['superseded','active'])
        self.assertEqual(list(PersonalMemoryIndexTask.objects.order_by('pk').values_list('status',flat=True)),['stale','queued'])
        current=read_current(self.user,r['memory_key']);self.assertEqual(current['candidate']['assertion'],'denied')
        self.assertEqual(current['candidate']['record']['revision'],2)
        self.assertEqual([v['previous_version'] for v in read_history(self.user,r['memory_key'])],[None,1])

    def test_stale_entry_version_does_not_create_third_revision(self):
        c,msg,r=self.save();new,source=self.correction()
        self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='correction')
        before=self.counts();new2,source2=self.correction()
        with self.assertRaisesRegex(PersonalMemoryStoreError,'stale_or_unavailable_target'):
            self.commit(new2,source2,target_key=r['memory_key'],expected_entry_version=1,intent='correction')
        self.assertEqual(self.counts(),before)

    def test_initial_transaction_rolls_back_when_enqueue_fails(self):
        c,msg=self.candidate()
        with patch('api.personal_memory_store._enqueue',side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):self.commit(c,msg)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_update_transaction_rolls_back_versions_and_job_states(self):
        c,msg,r=self.save();new,source=self.correction()
        with patch('api.personal_memory_store._enqueue',side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='correction')
        self.assertEqual(self.counts(),(1,1,1,1));self.assertEqual(PersonalMemoryEntry.objects.get().version,1)
        self.assertEqual(PersonalMemoryIndexTask.objects.get().status,'queued');self.assertIsNotNone(read_current(self.user,r['memory_key']))

    def test_receipt_failure_rolls_back_memory_and_index_task(self):
        c,msg=self.candidate()
        with patch('api.personal_memory_store.PersonalMemoryReceipt.objects.create',side_effect=RuntimeError('synthetic receipt failure')):
            with self.assertRaises(RuntimeError):self.commit(c,msg)
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_pause_stops_use_and_enqueues_delete_without_erasing_history(self):
        c,msg,r=self.save();new,source=self.candidate('停用我的高血压记录。',mode='explicit_request')
        self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='pause')
        self.assertIsNone(read_current(self.user,r['memory_key']));self.assertEqual(PersonalMemoryEntry.objects.get().status,'paused')
        self.assertEqual(PersonalMemoryIndexTask.objects.order_by('pk').last().operation,'delete')
        self.assertTrue(PersonalMemoryRevision.objects.get(version=1).candidate)

    def test_forget_scrubs_revisions_and_excludes_duplicate_sources(self):
        c,msg,r=self.save();duplicate,second=self.candidate()
        result=self.commit(duplicate,second,target_key=r['memory_key'],expected_entry_version=1)
        self.assertEqual(result['action'],'no_change');self.assertFalse(result['memory_changed'])
        new,source=self.candidate('忘记我的高血压记录。',mode='explicit_request')
        self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='forget')
        self.assertIsNone(read_current(self.user,r['memory_key']))
        self.assertTrue(all(v.candidate=={} for v in PersonalMemoryRevision.objects.all()))
        self.assertTrue(set([msg.pk,second.pk,source.pk])<=set(MemorySourceExclusion.objects.filter(user=self.user).values_list('message_id',flat=True)))
        self.assertTrue(ChatMessage.objects.filter(pk=msg.pk).exists())
        self.assertNotIn('高血压',json.dumps(read_history(self.user,r['memory_key']),default=str,ensure_ascii=False))
        self.assertEqual(PersonalMemoryIndexTask.objects.order_by('pk').last().operation,'delete')

    def test_replay_after_forget_never_restores_data_or_claims_current_usability(self):
        c,msg=self.candidate();r=self.commit(c,msg,request_key='original')
        new,source=self.candidate('忘记我的高血压记录。',mode='explicit_request')
        self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='forget');before=self.counts()
        replay=self.commit(c,msg,request_key='original')
        self.assertFalse(replay['currently_usable']);self.assertFalse(replay['writes_performed']);self.assertEqual(self.counts(),before)
        with self.assertRaisesRegex(PersonalMemoryStoreError,'source_excluded_or_reset'):self.commit(c,msg)

    def test_collection_off_still_allows_owned_explicit_forget(self):
        c,msg,r=self.save();self.policy=set_policy(self.user,expected_version=1,collect_types=[],history_conversation_ids=[],use_enabled=False)
        bad,source=self.candidate('忘记我爸的高血压记录。',mode='explicit_request',subject='family:father')
        with self.assertRaisesRegex(PersonalMemoryStoreError,'explicit_lifecycle_source_required'):
            self.commit(bad,source,target_key=r['memory_key'],expected_entry_version=1,intent='forget')
        new,source=self.candidate('忘记我的高血压记录。',mode='explicit_request')
        result=self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='forget')
        self.assertEqual(result['action'],'forget');self.assertEqual(PersonalMemoryEntry.objects.get().status,'forgotten')

    def test_history_requires_selected_conversation_but_ordinary_new_messages_do_not(self):
        c,msg=self.candidate()
        self.assertEqual(self.commit(c,msg,source_origin='history')['reason'],'history_not_selected')
        self.assertEqual(self.counts(),(0,0,0,0))
        self.policy=set_policy(self.user,expected_version=1,collect_types=list(TYPE_RULES),history_conversation_ids=[self.conv.pk],use_enabled=True)
        result=self.commit(c,msg,source_origin='history')
        self.assertEqual(read_current(self.user,result['memory_key'])['origin'],'automatic_history')

    def test_use_switch_and_disputed_state_prevent_current_read(self):
        c,msg,r=self.save();self.policy=set_policy(self.user,expected_version=1,collect_types=list(TYPE_RULES),history_conversation_ids=[],use_enabled=False)
        self.assertIsNone(read_current(self.user,r['memory_key']))
        self.policy=set_policy(self.user,expected_version=2,collect_types=list(TYPE_RULES),history_conversation_ids=[],use_enabled=True)
        self.assertIsNotNone(read_current(self.user,r['memory_key']))
        PersonalMemoryEntry.objects.filter(memory_key=r['memory_key']).update(status='disputed')
        self.assertIsNone(read_current(self.user,r['memory_key']))

    def test_unchanged_candidate_does_not_create_revision_or_task(self):
        c,msg,r=self.save();new,source=self.candidate()
        result=self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1)
        self.assertEqual(result['action'],'no_change');self.assertEqual(self.counts(),(1,1,2,1))

    def test_distinct_event_creates_separate_entry_not_overwrite(self):
        c,msg,r=self.save();new,source=self.candidate('我另一次有高血压相关自述。',event_id='distinct-event')
        result=self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1)
        self.assertEqual(result['action'],'keep_separate');self.assertNotEqual(result['memory_key'],r['memory_key'])
        self.assertEqual(PersonalMemoryEntry.objects.get(memory_key=r['memory_key']).version,1)

    def test_invalid_target_other_user_and_guest_do_not_leak(self):
        c,msg,r=self.save();self.assertIsNone(read_current(self.other,r['memory_key']));self.assertEqual(read_history(self.other,r['memory_key']),[])
        with self.assertRaisesRegex(PersonalMemoryStoreError,'login_required'):read_policy(AnonymousUser())
        new,source=self.correction()
        with self.assertRaisesRegex(PersonalMemoryStoreError,'stale_or_unavailable_target'):
            self.commit(new,source,target_key=uuid.uuid4().hex,expected_entry_version=1,intent='correction')

    def test_invalid_prior_source_prevents_update_without_partial_write(self):
        c,msg,r=self.save();new,source=self.correction();before=self.counts()
        ChatMessage.objects.filter(pk=msg.pk).update(content='已变化的旧来源')
        with self.assertRaises(PersonalMemoryStoreError):self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='correction')
        self.assertEqual(before,self.counts())

    def test_missing_target_revision_has_no_partial_write(self):
        c,msg,r=self.save();PersonalMemoryRevision.objects.all().delete()
        new,source=self.correction();before=self.counts()
        with self.assertRaisesRegex(PersonalMemoryStoreError,'target_revision_unavailable'):
            self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='correction')
        self.assertEqual(before,self.counts())

    def test_explicit_forget_still_works_after_old_source_was_deleted(self):
        c,msg,r=self.save();msg.delete()
        new,source=self.candidate('忘记我的高血压记录。',mode='explicit_request')
        result=self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='forget')
        self.assertEqual(result['action'],'forget')
        self.assertTrue(all(v.candidate=={} for v in PersonalMemoryRevision.objects.all()))

    def test_paused_entry_can_be_forgotten_with_current_version(self):
        c,msg,r=self.save();new,source=self.candidate('停用我的高血压记录。',mode='explicit_request')
        self.commit(new,source,target_key=r['memory_key'],expected_entry_version=1,intent='pause')
        new,source=self.candidate('忘记我的高血压记录。',mode='explicit_request')
        self.assertEqual(self.commit(new,source,target_key=r['memory_key'],expected_entry_version=2,intent='forget')['version'],3)
        self.assertEqual(PersonalMemoryEntry.objects.get().status,'forgotten')


class PersonalMemoryConcurrentTests(StoreFixtures, TransactionTestCase):
    def test_two_connections_cannot_both_replace_the_same_version(self):
        c,msg,r=self.save();a,ma=self.correction();b,mb=self.correction()
        barrier=threading.Barrier(2);results=[]
        def worker(candidate,source,request_key):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                result=commit_candidate(self.user,candidate,request_key=request_key,expected_policy_version=1,
                    source_conversation_version=0,expected_source_hash=self.hashes[source.pk],
                    target_key=r['memory_key'],expected_entry_version=1,intent='correction')
                results.append(result['status'])
            except PersonalMemoryStoreError as error:results.append(str(error))
            except Exception as error:results.append('unexpected:'+type(error).__name__)
            finally:close_old_connections()
        threads=[threading.Thread(target=worker,args=(a,ma,'concurrent-a')),threading.Thread(target=worker,args=(b,mb,'concurrent-b'))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads),results)
        self.assertEqual(results.count('committed'),1,results)
        self.assertEqual(len(results),2,results)
        self.assertTrue(all(x in {'committed','stale_or_unavailable_target','write_conflict_retry_same_request'} for x in results),results)
        self.assertEqual(PersonalMemoryEntry.objects.get().version,2)
        self.assertEqual(PersonalMemoryRevision.objects.count(),2)
        self.assertEqual(PersonalMemoryIndexTask.objects.count(),2)
