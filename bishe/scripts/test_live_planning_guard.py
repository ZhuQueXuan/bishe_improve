import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import evaluate_query_planning_live as live
from api.query_planning import SYSTEM_PROMPT


class LiveGuardTests(unittest.TestCase):
    def setUp(self):
        response = SimpleNamespace(model='deepseek-flash', usage=SimpleNamespace(prompt_tokens=10,completion_tokens=10),
                                   choices=[SimpleNamespace(finish_reason='stop')])
        self.create = Mock(return_value=response)
        self.client = Mock()
        self.client.base_url = 'https://api.deepseek.com/'
        self.client.with_options.return_value.chat.completions.create = self.create
        self.recorder = live.Recorder(self.client, ['糖尿病有什么征兆？'])
        journal = patch.object(live, 'JOURNAL', Mock())
        journal.start()
        self.addCleanup(journal.stop)
        self.kwargs = {'model':'deepseek-flash','max_tokens':600,'timeout':15,'stream':False,
                       'messages':[{'role':'system','content':SYSTEM_PROMPT},
                                   {'role':'user','content':json.dumps({'question':'糖尿病有什么征兆？'})}]}

    def test_single_safe_call_records_usage(self):
        self.recorder.create(**self.kwargs)
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0]['usage']['prompt_tokens'], 10)
        self.assertLess(self.recorder.reserved * 8, 1)

    def test_call_cap_blocks_before_external_request(self):
        self.recorder.calls = [{}] * 12
        with self.assertRaises(ValueError):
            self.recorder.create(**self.kwargs)
        self.create.assert_not_called()

    def test_budget_blocks_before_external_request(self):
        self.recorder.reserved = 1/8
        with self.assertRaises(ValueError):
            self.recorder.create(**self.kwargs)
        self.create.assert_not_called()

    def test_history_and_unknown_question_blocked(self):
        self.kwargs['messages'][1]['content'] = json.dumps({'question':'糖尿病有什么征兆？','history':['私密信息']})
        with self.assertRaises(ValueError):
            self.recorder.create(**self.kwargs)
        self.create.assert_not_called()

    def test_destination_blocked(self):
        self.client.base_url = 'https://unapproved.example/'
        with self.assertRaises(ValueError):
            self.recorder.create(**self.kwargs)
        self.create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
