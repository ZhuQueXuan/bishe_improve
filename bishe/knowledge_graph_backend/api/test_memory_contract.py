"""Stage A structured semantics: does not test language understanding or chat."""
import copy
import json
from pathlib import Path
from django.test import SimpleTestCase
from .memory_contract import validate_record,decide_update,MemoryContractError,FIELDS
from .personal_memory_contract import validate_candidate,decide_personal_update,TYPE_RULES


class PersonalAutomaticPolicyTests(SimpleTestCase):
    """P01 structured decisions only; no natural-language or storage claim."""
    def setUp(self):
        self.messages={}

    def candidate(self, rid, text='我有高血压。', category='self_disease_report', assertion='reported',
                  value='高血压', details=None, **changes):
        source_changes=changes.pop('source',{})
        kind=TYPE_RULES[category][0]
        event=kind=='event'
        row={'id':rid,'owner':'synthetic-A','session':'synthetic-session','subject':'self',
             'kind':kind,'attribute':category+':'+value if event else 'length','value':value,
             'scope':'long_term','scope_ref':None,'storage_state':'active',
             'event_state':'current' if event else 'not_applicable','event_id':'event-one' if event else None,
             'recorded_at':'2026-10-27T12:00:00+08:00','event_time':{'raw':None,'start':None,'end':None},
             'source':{'message_id':'m-'+rid,'start':0,'end':len(text),'text':text,'mode':'self_report'},'revision':1}
        row.update(changes);row['source'].update(source_changes)
        if row['scope']=='current' and row['scope_ref'] is None:row['scope_ref']=row['source']['message_id']
        self.messages[row['source']['message_id']]={'id':row['source']['message_id'],'owner':row['owner'],
            'session':row['session'],'content':text,'role':'user','recorded_at':row['recorded_at']}
        return {'category':category,'assertion':assertion if event else 'not_applicable','record':row,'details':details or {}}

    def decide(self,new,old=None,**kw):
        return decide_personal_update(new,self.messages,owner='synthetic-A',allowed_types=tuple(TYPE_RULES),old=old,**kw)

    def target(self,old):
        return {'target_id':old['record']['id'],'expected_revision':old['record']['revision']}

    def test_supported_type_fields_and_no_clinical_or_user_confirmation(self):
        examples=[
            ('self_disease_report','我有高血压。','高血压',{}),
            ('self_allergy_report','我对虚构药甲过敏，起疹子。','虚构药甲',{'reaction':'起疹子'}),
            ('self_intolerance_report','我吃虚构药甲不耐受，恶心。','虚构药甲',{'reaction':'恶心'}),
            ('current_medication','我服虚构药甲，每次5毫克，每天一次。','虚构药甲',{'dose_raw':'5毫克','frequency_raw':'每天一次'}),
            ('temporary_health_state','我目前在哺乳期。','哺乳期',{}),
            ('symptom_event','我昨天发烧了。','发烧',{}),
            ('measurement_event','我测了血压135/85毫米汞柱。','血压',{'value_raw':'135/85','unit':'毫米汞柱'}),
            ('test_result_event','我的虚构检查甲结果阴性。','虚构检查甲',{'result_raw':'阴性'}),
            ('presentation_preference','以后简短点。','brief',{}),
            ('service_relevant_background','我是护理学生。','nursing',{}),
        ]
        for category,text,value,details in examples:
            with self.subTest(category=category):
                extra={'attribute':'background'} if category=='service_relevant_background' else {}
                c=self.candidate(category,text,category,value=value,details=details,**extra)
                out=self.decide(c)
                self.assertEqual(out['action'],'add')
                self.assertFalse(out['writes_performed']);self.assertFalse(out['clinical_confirmation'])
                self.assertFalse(out['user_confirmed'])

    def test_suspected_denied_and_observation_do_not_become_diagnosis(self):
        for assertion,state,text in [('suspected','uncertain','我可能有高血压。'),('denied','denied','我没有高血压。')]:
            c=self.candidate(assertion,text,assertion=assertion,event_state=state)
            self.assertEqual(self.decide(c)['assertion'],assertion)
            c['assertion']='reported'
            with self.assertRaisesRegex(MemoryContractError,'assertion_state'):validate_candidate(c,self.messages)
        disease=self.candidate('disease')
        measurement=self.candidate('measurement','我最近测的血压为120/80毫米汞柱。','measurement_event',value='血压',
                                   details={'value_raw':'120/80','unit':'毫米汞柱'})
        self.assertEqual(self.decide(measurement,disease)['action'],'keep_separate')

    def test_unasserted_family_unknown_and_temporary_are_not_long_term_self_facts(self):
        for mode in ['question','hypothetical','quotation','assistant_inference']:
            c=self.candidate(mode,source={'mode':mode})
            self.assertEqual(self.decide(c)['action'],'reject')
        family=self.candidate('family','我爸有高血压。',subject='family:father',source={'mode':'family_report'})
        self.assertEqual(self.decide(family)['action'],'reject')
        unknown=self.candidate('unknown',source={'mode':'explicit_request'},subject='unknown')
        self.assertEqual(self.decide(unknown)['action'],'clarify')
        short=self.candidate('short','这次简短点。','presentation_preference',value='brief',scope='current')
        self.assertEqual(self.decide(short)['action'],'temporary_only')

    def test_type_grant_and_history_selection_cannot_be_supplied_by_candidate(self):
        c=self.candidate('new')
        out=decide_personal_update(c,self.messages,owner='synthetic-A')
        self.assertEqual(out['reason'],'collection_not_enabled')
        self.assertEqual(self.decide(c,source_origin='history')['reason'],'history_not_selected')
        self.assertEqual(self.decide(c,source_origin='history',history_sessions=['synthetic-session'])['action'],'add')
        with self.assertRaisesRegex(MemoryContractError,'source_origin'):self.decide(c,source_origin=[])
        for policy in [dict(allowed_types=True),dict(allowed_types=['unsupported'])]:
            with self.assertRaises(MemoryContractError):
                decide_personal_update(c,self.messages,owner='synthetic-A',**policy)
        c['allowed_types']=list(TYPE_RULES)
        with self.assertRaisesRegex(MemoryContractError,'candidate_fields'):validate_candidate(c,self.messages)

    def test_cross_account_and_missing_tampered_assistant_sources(self):
        c=self.candidate('foreign',owner='synthetic-B')
        self.assertEqual(self.decide(c)['reason'],'different_owner')
        c=self.candidate('source')
        for field,value in [('role','assistant'),('content','不匹配的原话'),('recorded_at','2026-10-20T12:00:00+08:00')]:
            with self.subTest(field=field):
                messages=copy.deepcopy(self.messages);messages['m-source'][field]=value
                with self.assertRaises(MemoryContractError):validate_candidate(c,messages)
        with self.assertRaisesRegex(MemoryContractError,'source_missing'):validate_candidate(c,{})

    def test_details_are_type_specific_literal_and_required_for_observations(self):
        c=self.candidate('dose','我服虚构药甲。','current_medication',value='虚构药甲',details={'dose_raw':'5毫克'})
        with self.assertRaisesRegex(MemoryContractError,'detail_source'):validate_candidate(c,self.messages)
        c['details']={'diagnosis':'高血压'}
        with self.assertRaisesRegex(MemoryContractError,'details_fields'):validate_candidate(c,self.messages)
        c=self.candidate('value','我测了血压。','measurement_event',value='血压')
        with self.assertRaisesRegex(MemoryContractError,'details_fields'):validate_candidate(c,self.messages)
        c=self.candidate('unsupported_value');c['record']['value']='糖尿病'
        with self.assertRaisesRegex(MemoryContractError,'value_source'):validate_candidate(c,self.messages)

    def test_same_event_change_requires_exact_target_and_current_revision(self):
        old=self.candidate('old');new=self.candidate('new','我之前说的高血压情况已结束。',event_state='resolved')
        for args in [{},{'target_id':'other','expected_revision':1},{'target_id':'old','expected_revision':2},
                     {'target_id':'old','expected_revision':True}]:
            self.assertEqual(self.decide(new,old,intent='same_event_update',**args)['action'],'clarify')
        self.assertEqual(self.decide(new,old,intent='same_event_update',**self.target(old))['action'],'event_transition')
        new['record']['event_id']=None
        self.assertEqual(self.decide(new,old,intent='same_event_update',**self.target(old))['reason'],'event_identity_required')

    def test_new_event_and_unknown_identity_are_not_latest_wins(self):
        old=self.candidate('old','我2025年感冒了。','symptom_event',value='感冒',event_id='event-2025',
                           event_time={'raw':'2025年','start':None,'end':None},event_state='past')
        new=self.candidate('new','我2026年又感冒了。','symptom_event',value='感冒',event_id='event-2026',
                           event_time={'raw':'2026年','start':None,'end':None})
        self.assertEqual(self.decide(new,old)['reason'],'distinct_events')
        new['record']['event_id']=None
        self.assertEqual(self.decide(new,old)['reason'],'event_identity_required')

    def test_explicit_correction_and_broad_denial_conflict(self):
        old=self.candidate('old');new=self.candidate('new','刚才说错了，我没有高血压。',assertion='denied',
                                                   event_state='denied',source={'mode':'explicit_request'})
        self.assertEqual(self.decide(new,old)['reason'],'state_conflict_requires_relationship')
        self.assertEqual(self.decide(new,old,intent='correction',**self.target(old))['action'],'replace_effective')
        self.assertEqual(self.decide(new,old,intent='new_default',**self.target(old))['action'],'reject')
        new['record']['source']['mode']='self_report'
        self.assertEqual(self.decide(new,old,intent='correction',**self.target(old))['action'],'clarify')
        allergy=self.candidate('allergy','我对虚构药甲过敏。','self_allergy_report',value='虚构药甲')
        broad=self.candidate('broad','我没有过敏。','self_allergy_report',assertion='denied',value='过敏',event_state='denied')
        self.assertEqual(self.decide(broad,allergy,intent='correction',**self.target(allergy))['action'],'clarify')

    def test_older_source_cannot_override_by_later_ingestion(self):
        old=self.candidate('old');new=self.candidate('new','以前我没有高血压。',assertion='denied',event_state='denied',
                                                   recorded_at='2023-10-27T12:00:00+08:00',source={'mode':'explicit_request'})
        self.assertEqual(self.decide(new,old,intent='correction',**self.target(old))['reason'],'older_source_not_current_update')

    def test_additional_medication_details_preserve_old_fields(self):
        old=self.candidate('old','我服虚构药甲，每次5毫克。','current_medication',value='虚构药甲',details={'dose_raw':'5毫克'})
        new=self.candidate('new','我服虚构药甲，每次5毫克，每天一次。','current_medication',value='虚构药甲',
                           details={'dose_raw':'5毫克','frequency_raw':'每天一次'})
        self.assertEqual(self.decide(new,old,intent='supplement',**self.target(old))['action'],'supplement')
        new['details'].pop('dose_raw')
        self.assertEqual(self.decide(new,old,intent='supplement',**self.target(old))['action'],'clarify')

    def test_preferences_use_explicit_default_and_temporary_does_not_replace(self):
        old=self.candidate('old','以后详细点。','presentation_preference',value='detailed',source={'mode':'explicit_request'})
        new=self.candidate('new','以后简短点。','presentation_preference',value='brief',source={'mode':'explicit_request'})
        self.assertEqual(self.decide(new,old)['action'],'clarify')
        self.assertEqual(self.decide(new,intent='new_default')['action'],'add')
        self.assertEqual(self.decide(new,old,intent='new_default')['action'],'clarify')
        self.assertEqual(self.decide(new,old,intent='new_default',**self.target(old))['action'],'replace_effective')
        new['record'].update(scope='current',scope_ref='m-new')
        self.assertEqual(self.decide(new,old,intent='new_default',**self.target(old))['action'],'temporary_only')

    def test_forget_is_allowed_with_collection_off_but_needs_matching_target(self):
        old=self.candidate('old');new=self.candidate('new','忘记我的高血压记录。',source={'mode':'explicit_request'})
        out=decide_personal_update(new,self.messages,owner='synthetic-A',old=old,intent='forget',**self.target(old))
        self.assertEqual(out['action'],'forget')
        wrong=self.candidate('wrong','忘记我的糖尿病记录。',value='糖尿病',source={'mode':'explicit_request'})
        self.assertEqual(self.decide(wrong,old,intent='forget',**self.target(old))['reason'],'lifecycle_target_mismatch')

    def test_exclusions_forgotten_and_inactive_cannot_be_revived(self):
        old=self.candidate('old');new=self.candidate('new')
        self.assertEqual(self.decide(new,old,excluded_sources=['m-new'])['reason'],'excluded_source')
        self.assertEqual(self.decide(new,old,excluded_sources=['m-old'])['reason'],'excluded_target_source')
        old['record']['storage_state']='forgotten'
        replay=copy.deepcopy(old);replay['record'].update(id='replay',storage_state='active')
        self.assertEqual(self.decide(replay,old)['reason'],'forgotten_source_replay')
        self.assertEqual(self.decide(new,old)['reason'],'inactive_target')

    def test_results_are_detached_and_inputs_never_mutated(self):
        c=self.candidate('new');before=copy.deepcopy((c,self.messages))
        out=self.decide(c);validated=validate_candidate(c,self.messages)
        validated['record']['source']['text']='changed'
        self.assertEqual(before,(c,self.messages));self.assertEqual(out['action'],'add')
        self.assertEqual(self.decide(copy.deepcopy(c),c)['reason'],'identical_replay')

    def test_shape_check_is_not_language_understanding(self):
        # As in the legacy contract, valid text offsets do not prove meaning.
        c=self.candidate('not_semantics','如果我有高血压怎么办？')
        self.assertEqual(validate_candidate(c,self.messages)['assertion'],'reported')
        # P03 must supply the hypothetical mode correctly; then policy rejects.
        c['record']['source']['mode']='hypothetical'
        self.assertEqual(self.decide(c)['action'],'reject')


class MemoryContractTests(SimpleTestCase):
    def setUp(self):self.messages={}

    def record(self,rid,text,**changes):
        source_changes=changes.pop('source',{})
        r={'id':rid,'owner':'synthetic-user','session':'synthetic-session','subject':'self',
           'kind':'preference','attribute':'response.length','value':'detailed',
           'scope':'long_term','scope_ref':None,'storage_state':'active','event_state':'not_applicable',
           'event_id':None,'recorded_at':'2026-10-24T08:00:00+08:00',
           'event_time':{'raw':None,'start':None,'end':None},
           'source':{'message_id':'m-'+rid,'start':0,'end':len(text),'text':text,'mode':'explicit_request'},'revision':1}
        r.update(changes);r['source'].update(source_changes)
        if r['scope']=='current' and r['scope_ref'] is None:r['scope_ref']=r['source']['message_id']
        if r['scope']=='session' and r['scope_ref'] is None:r['scope_ref']=r['session']
        self.messages[r['source']['message_id']]={'id':r['source']['message_id'],'owner':r['owner'],'session':r['session'],
            'content':text,'role':'user','recorded_at':r['recorded_at']}
        return r

    def event(self,rid,text,**changes):
        return self.record(rid,text,**{'kind':'event','attribute':'health.cold','value':'用户自述的一次感冒',
            'scope':'session','event_state':'current','event_id':'event-one',**changes})

    def decide(self,old,new,intent='new_information'):
        return decide_update(old,new,self.messages,intent=intent)['action']

    def test_declared_scenarios_and_non_mutating_decisions(self):
        old=self.record('old','以后详细一点。')
        def preference(rid,text,**kw):return self.record(rid,text,**kw)
        current=self.event('current','我这次感冒了。')
        forgotten=copy.deepcopy(old);forgotten['storage_state']='forgotten'
        replay=copy.deepcopy(old);replay['id']='replay'
        cases={
          'A01':(old,preference('brief','这次简短点。',value='brief',scope='current'),'new_information'),
          'A02':(old,preference('default','以后简短点。',value='brief'),'new_default'),
          'A03':(current,self.event('mother','我妈妈感冒了。',subject='family:mother',source={'mode':'family_report'}),'new_information'),
          'A04':(current,self.event('resolved','我这次感冒已经好了。',event_state='resolved'),'same_event_update'),
          'A05':(current,self.event('unlinked','我那次感冒好了。',event_state='resolved',event_id=None),'same_event_update'),
          'A06':(preference('sweet','我喜欢甜咖啡。',attribute='taste.coffee',value='sweet'),
                 preference('sugar','我目前戒糖。',kind='constraint',attribute='diet.sugar',value='avoid_sugar'),'new_information'),
          'A07':(old,preference('ambiguous','今天简短。',value='brief'),'new_information'),
          'A08':(old,preference('correct','更正，以后简短。',value='brief'),'correction'),
          'A09':(current,self.event('past','我上周感冒了。',event_state='past',event_id=None,
                 event_time={'raw':'上周','start':None,'end':None}),'new_information'),
          'A10':(None,preference('quoted','“以后简短点”',value='brief',source={'mode':'quotation'}),'new_information'),
          'A11':(forgotten,replay,'new_information'),
          'A12':(old,preference('foreign','更正，以后简短。',value='brief',owner='another-user'),'correction'),
          'A13':(current,self.event('different','我另一次感冒了。',event_id='event-two'),'new_information'),
          'A14':(old,preference('unknown','更正，以后简短。',value='brief',subject='unknown'),'correction'),
          'A16':(old,preference('later','我喜欢简短。',value='brief',recorded_at='2026-10-25T08:00:00+08:00'),'new_information'),
          'A17':(current,self.event('improved','我那次感冒好转了。',event_state='uncertain'),'same_event_update'),
          'A18':(old,preference('forget','忘记我的篇幅偏好。'),'forget'),
          'A19':(old,preference('temp-correct','更正，这次简短。',value='brief',scope='current'),'correction'),
          'A20':(preference('turn-old','这次简短。',value='brief',scope='current'),
                 preference('turn-new','这次详细。',scope='current',session='another-session'),'new_information')}
        acceptance=json.loads((Path(__file__).resolve().parents[2]/'data/memory_system_v1/acceptance.json').read_text(encoding='utf-8'))
        declared={c['id']:c for c in acceptance['cases']}
        self.assertEqual(set(cases)|{'A15'},set(declared))
        for key,(previous,new,intent) in cases.items():
            with self.subTest(key=key):
                before=copy.deepcopy((previous,new,self.messages))
                result=decide_update(previous,new,self.messages,intent=intent)
                self.assertEqual(result['action'],declared[key]['expected'])
                self.assertFalse(result['writes_performed']);self.assertFalse(result['clinical_confirmation'])
                self.assertEqual((previous,new,self.messages),before)
        bad=preference('bad-span','以后详细点。');bad['source']['text']='以后简短点。'
        with self.assertRaisesRegex(MemoryContractError,'source_span'):validate_record(bad,self.messages)

    def test_declared_field_contract_matches_validator(self):
        schema=json.loads((Path(__file__).resolve().parents[2]/'data/memory_system_v1/contract.json').read_text(encoding='utf-8'))
        self.assertEqual(set(schema['record_fields']),FIELDS)
        self.assertFalse(schema['production_connected'])

    def test_unicode_source_offsets_are_exact_and_not_encoded_byte_offsets(self):
        r=self.record('offset','说明：以后详细点。')
        r['source'].update(start=3,text='以后详细点。')
        self.assertEqual(validate_record(r,self.messages),r)
        r['source']['start']=len('说明：'.encode('utf-8'))
        with self.assertRaises(MemoryContractError):validate_record(r,self.messages)

    def test_non_user_and_cross_account_sources_are_rejected(self):
        r=self.record('r','以后详细点。')
        self.messages['m-r']['role']='assistant'
        with self.assertRaisesRegex(MemoryContractError,'source_user'):validate_record(r,self.messages)
        self.messages['m-r']['role']='user';self.messages['m-r']['owner']='other'
        with self.assertRaisesRegex(MemoryContractError,'source_partition'):validate_record(r,self.messages)

    def test_unknown_extra_fields_and_wrong_revision_types_rejected(self):
        r=self.record('r','以后详细点。');r['confidence']=0.99
        with self.assertRaisesRegex(MemoryContractError,'record_fields'):validate_record(r,self.messages)
        r.pop('confidence');r['revision']=True
        with self.assertRaisesRegex(MemoryContractError,'revision'):validate_record(r,self.messages)

    def test_clinical_event_state_is_separate_from_storage_state(self):
        r=self.event('r','我感冒好了。',event_state='resolved')
        self.assertEqual(validate_record(r,self.messages)['storage_state'],'active')
        r['storage_state']='resolved'
        with self.assertRaisesRegex(MemoryContractError,'storage_state'):validate_record(r,self.messages)

    def test_time_requires_timezone_source_and_ordered_interval(self):
        r=self.event('r','我今天感冒了。')
        r['recorded_at']='2026-10-24T08:00:00'
        with self.assertRaisesRegex(MemoryContractError,'timestamp_timezone'):validate_record(r,self.messages)
        r['recorded_at']='2026-10-24T08:00:00+08:00'
        r['event_time']={'raw':'上周','start':None,'end':None}
        with self.assertRaisesRegex(MemoryContractError,'event_time_source'):validate_record(r,self.messages)
        r['event_time']={'raw':'今天','start':'2026-10-24T09:00:00+08:00','end':'2026-10-24T08:00:00+08:00'}
        with self.assertRaisesRegex(MemoryContractError,'event_time_interval'):validate_record(r,self.messages)

    def test_record_time_must_match_trusted_source_message(self):
        r=self.record('r','以后详细点。');r['recorded_at']='2026-10-25T08:00:00+08:00'
        with self.assertRaisesRegex(MemoryContractError,'recorded_at_source'):validate_record(r,self.messages)

    def test_source_mode_cannot_claim_family_as_self(self):
        r=self.event('r','我妈妈感冒了。',subject='family:mother',source={'mode':'self_report'})
        with self.assertRaisesRegex(MemoryContractError,'self_report_subject'):validate_record(r,self.messages)

    def test_update_requires_explicit_intent_evidence_and_event_link(self):
        a=self.event('a','我感冒了。');b=self.event('b','我感冒已经好了。',event_state='resolved',source={'mode':'self_report'})
        self.assertEqual(self.decide(a,b,'same_event_update'),'clarify')
        b['source']['mode']='explicit_request';b['event_id']='another-event'
        self.assertEqual(self.decide(a,b,'same_event_update'),'clarify')

    def test_inactive_memories_cannot_be_silently_reenabled(self):
        a=self.record('a','以后详细点。',storage_state='paused');b=self.record('b','以后简短点。',value='brief')
        self.assertEqual(self.decide(a,b,'new_default'),'clarify')

    def test_question_hypothetical_and_assistant_inference_are_not_memories(self):
        for mode in ['question','hypothetical','quotation','assistant_inference']:
            r=self.record(mode,'合成候选文字。',source={'mode':mode})
            self.assertEqual(self.decide(None,r),'reject')

    def test_record_id_is_immutable_but_identical_replay_is_noop(self):
        a=self.record('a','以后详细点。')
        self.assertEqual(self.decide(a,copy.deepcopy(a)),'no_change')
        b=copy.deepcopy(a);b['value']='brief'
        self.assertEqual(self.decide(a,b),'reject')

    def test_missing_targets_and_wrong_subject_are_not_modified(self):
        b=self.record('b','更正，以后简短点。',value='brief')
        self.assertEqual(self.decide(None,b,'correction'),'clarify')
        a=self.record('a','以后详细点。',subject='family:mother')
        self.assertEqual(self.decide(a,b,'correction'),'clarify')

    def test_session_scope_ref_matches_source_conversation(self):
        a=self.record('a','本会话简短点。',value='brief',scope='session',scope_ref='wrong')
        with self.assertRaisesRegex(MemoryContractError,'scope_ref_session'):validate_record(a,self.messages)

    def test_contract_does_not_claim_to_validate_language_semantics(self):
        # Deliberately wrong supplied meaning: raw span verification alone cannot
        # establish a natural-language value. Stage B must reject this extraction.
        a=self.record('a','以后详细点。',value='brief')
        self.assertEqual(validate_record(a,self.messages)['value'],'brief')

    def test_delayed_old_source_is_not_an_authorized_current_update(self):
        old=self.record('a','以后详细点。',recorded_at='2026-10-25T08:00:00+08:00')
        new=self.record('b','以后简短点。',value='brief')
        self.assertEqual(self.decide(old,new,'new_default'),'clarify')

    def test_active_value_is_not_empty_and_validation_returns_detached_data(self):
        row=self.record('a','以后详细点。')
        validated=validate_record(row,self.messages);validated['source']['text']='changed'
        self.assertNotEqual(row['source']['text'],validated['source']['text'])
        row['value']=''
        with self.assertRaisesRegex(MemoryContractError,'value'):validate_record(row,self.messages)
