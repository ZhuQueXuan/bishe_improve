"""Preference restoration keeps monotonic versions and existing source guards."""
import unittest
from .test_natural_memory_policy import Harness,preference
from .natural_memory_contract import NaturalMemoryContractError

class PreferenceUndoTests(unittest.TestCase):
    def changed(self):
        h=Harness();h.policy.update(preference_auto_enabled=True,preference_auto_gate_passed=True)
        source=h.source('以后回答简短一些。');h.stage(source,[preference(source)]);key=next(iter(h.state['entries']))
        source=h.source('以后回答详细一些。');fact=preference(source,'detailed')
        h.stage(source,[fact],choices=h.choose(source,fact,'new_default',key));h.accept();return h,key
    def test_restore_previous_value_with_new_monotonic_revision(self):
        h,key=self.changed();before=h.state['revision'];version=h.state['entries'][key]['version']
        h.change('undo',key);entry=h.state['entries'][key]
        self.assertEqual(entry['fact']['content']['value'],'brief');self.assertGreater(entry['version'],version)
        self.assertGreater(h.state['revision'],before);self.assertEqual(entry['history'][-1]['fact']['content']['value'],'detailed')
    def test_undo_does_not_restore_forgotten_preference(self):
        h,key=self.changed();h.change('undo',key);h.change('forget',key)
        with self.assertRaisesRegex(NaturalMemoryContractError,'already_forgotten'):h.change('undo',key)
        self.assertIsNone(h.state['entries'][key]['fact']);self.assertEqual(h.state['entries'][key]['history'],[])
    def test_changed_original_source_cannot_be_restored(self):
        h,key=self.changed();old=h.state['entries'][key]['history'][-1]['bindings'][0]['messages'][0]['id']
        h.live[old]={**h.live[old],'content':'虚构来源已被改动'}
        with self.assertRaises(NaturalMemoryContractError):h.change('undo',key)
    def test_health_cannot_use_preference_undo(self):
        h=Harness();key=h.saved()
        with self.assertRaisesRegex(NaturalMemoryContractError,'undo_preference_only'):h.change('undo',key)
    def test_no_changed_value_has_no_undo(self):
        h=Harness();source=h.source('以后回答简短一些。');h.stage(source,[preference(source)]);h.accept()
        with self.assertRaisesRegex(NaturalMemoryContractError,'undo_history_required'):h.change('undo')
