import json
import unittest

from . import graph_retrieval as graph


PROOF = {'file': 'bishe/data/medical.json', 'file_sha256': 'a' * 64,
         'line': 1, 'disease': '糖尿病', 'field': 'symptom', 'raw_value': '多尿'}


class FakeTransaction:
    def __init__(self, state='complete', source=None):
        self.state, self.source, self.texts = state, source, []
    def run(self, query, **params):
        if query == graph.MARKER_QUERY:
            return [{'state': self.state, 'plan_sha256': 'test'}] if self.state else []
        if query == graph.DISEASE_QUERY:
            self.texts.append(params['text'])
            return [{'id': 'd', 'name': '糖尿病', 'properties_json': '{}', 'property_sources_json': '{}'}] if '糖尿病' in params['text'] else []
        if query == graph.RELATION_QUERY:
            return [{'kind': 'has_symptom', 'name': '多尿', 'sources_json': json.dumps([self.source or PROOF])}]
        raise AssertionError('Unexpected query')


class RetrievalTests(unittest.TestCase):
    def test_natural_question_and_evidence(self):
        result = graph.retrieve_transaction(FakeTransaction(), '糖尿病有哪些症状？')
        self.assertEqual(result['facts'][0]['value'], '多尿')
        self.assertEqual(result['facts'][0]['sources'][0]['line'], 1)
        self.assertIn('[E1]', result['context'])

    def test_incomplete_or_missing_dataset_refused_before_entity_query(self):
        for state in ('importing', None):
            tx = FakeTransaction(state)
            with self.assertRaises(graph.RetrievalUnavailable):
                graph.retrieve_transaction(tx, '糖尿病症状')
            self.assertEqual(tx.texts, [])

    def test_followup_uses_user_history_only(self):
        result = graph.retrieve_transaction(FakeTransaction(), '它有哪些症状？',
                                            [{'role': 'user', 'content': '糖尿病是什么？'},
                                             {'role': 'assistant', 'content': '高血压'}])
        self.assertEqual(result['resolution'], 'recent_user_question')
        self.assertEqual(result['diseases'], ['糖尿病'])

    def test_unrelated_question_does_not_reuse_history(self):
        result = graph.retrieve_transaction(FakeTransaction(), 'XYZ病有哪些症状？', [{'role': 'user', 'content': '糖尿病'}])
        self.assertEqual(result['status'], 'no_evidence')
        self.assertEqual(result['facts'], [])

    def test_changed_raw_value_refused(self):
        with self.assertRaises(graph.RetrievalUnavailable):
            graph.retrieve_transaction(FakeTransaction(source={**PROOF, 'raw_value': '伪造内容'}), '糖尿病症状')

    def test_intent_limits_relevant_categories(self):
        self.assertEqual(graph.intent('糖尿病病因和预防'), ([], ['cause', 'prevent']))
        self.assertEqual(graph.intent('糖尿病检查'), (['need_check'], []))
        self.assertEqual(graph.intent('糖尿病用药'), (['common_drug', 'recommand_drug'], []))

    def test_sources_displayed_without_hidden_prompt(self):
        facts = graph.retrieve_transaction(FakeTransaction(), '糖尿病症状')['facts']
        self.assertIn('medical.json 第1行', graph.source_appendix(facts))
        self.assertEqual(graph.source_appendix([]), '')

    def test_factory_or_brand_request_has_no_generated_evidence(self):
        result = graph.retrieve_transaction(FakeTransaction(), '糖尿病药品厂家和品牌是什么？')
        self.assertEqual(result['status'], 'unsupported_field')
        self.assertEqual(result['facts'], [])
