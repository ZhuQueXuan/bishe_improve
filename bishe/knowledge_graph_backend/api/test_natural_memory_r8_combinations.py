"""R8 engineering regression: reviewed self-attribution withdrawal blocks use."""
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from django.db import connection
from django.test import TestCase, override_settings
from . import natural_memory_intake as intake
from . import test_natural_memory_intake as fixtures
from .test_natural_memory_policy import Harness
from .test_natural_memory_contract import disease

class RetractionCombinationTests(unittest.TestCase):
    def test_reviewed_retraction_stops_old_use_before_target_resolution(self):
        h=Harness();source=h.source('我患有银屑病。');h.stage(source,[disease(source['messages'][-1])]);h.accept()
        old=next(iter(h.state['entries']));quote=h.state['entries'][old]['sources'][0]['text']
        reply=h.source('刚才说的其实是我妈妈，不是我。',context=source['messages'])
        candidate=disease(reply['messages'][-1]);candidate['relation']='retraction'
        candidate['evidence']=[{'source_ref':'context_1','text':quote},
            {'source_ref':'current','text':reply['messages'][-1]['content']}]
        result=h.stage(reply,[candidate])
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(h.eligible(),[])
        self.assertEqual(h.state['entries'][old]['status'],'disputed')
        self.assertEqual(h.state['entries'][old]['sources'][0]['text'],quote)
        self.assertEqual(len(h.state['entries']),1)
        self.assertFalse(h.state['entries'][old]['clinical_confirmation'])
        h.accept(decision='reject')
        self.assertEqual(h.eligible(),[])
        h.change('keep_original',old)
        self.assertEqual(len(h.eligible()),1)


@override_settings(ROOT_URLCONF='api.natural_memory_fixture_urls',NATURAL_MEMORY_ISOLATED_ENABLED=True,
    NATURAL_MEMORY_ISOLATED_DATABASE='memory')
class SourceTimezoneCombinationTests(TestCase):
    setUp=fixtures.NaturalMemoryIntakeTests.setUp
    key=fixtures.NaturalMemoryIntakeTests.key
    versions=fixtures.NaturalMemoryIntakeTests.versions
    message_body=fixtures.NaturalMemoryIntakeTests.message_body

    def test_new_message_source_matches_non_utc_database_readback(self):
        with patch.object(connection,'timezone',ZoneInfo('Asia/Shanghai')):
            accepted=intake.post_message(self.user,self.message_body())
            request=intake.worker_request(self.user,accepted['job_key'])
            self.assertEqual(request['stage'],'extraction')

    def test_unrelated_report_is_not_disputed(self):
        h=Harness();old=h.saved()
        source=h.source('撤回我之前说的高血压自述。')
        candidate=disease(source['messages'][-1],'高血压');candidate['relation']='retraction'
        h.stage(source,[candidate])
        self.assertEqual(h.state['entries'][old]['status'],'active')
        self.assertEqual(len(h.eligible()),1)
