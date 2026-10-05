import json,uuid,unittest
from pathlib import Path
from types import SimpleNamespace
from .authorized_planner import BatchLedger,RESERVE,TASKS
from .planning_runtime import BoundaryError
from .test_planning_runtime import TEMP

class BatchBudgetTests(unittest.TestCase):
    def setUp(self):self.ledger=BatchLedger(TEMP/('batch_test_'+uuid.uuid4().hex+'.json'),TEMP)
    def test_batch_16_and_per_task_4(self):
        for task in TASKS:
            for _ in range(4):
                e=self.ledger.reserve(task);self.ledger.settle(e,{'prompt_tokens':100,'completion_tokens':30,'total_tokens':130})
        with self.assertRaises(BoundaryError):self.ledger.reserve('P01')
        self.assertEqual(len(self.ledger.data['calls']),16)
    def test_per_task_cap_before_batch_exhausted(self):
        for _ in range(4):self.ledger.reserve('P01')
        with self.assertRaises(BoundaryError):self.ledger.reserve('P01')
        self.ledger.reserve('P02')
    def test_unknown_usage_halts_future_requests(self):
        e=self.ledger.reserve('P01');self.ledger.settle(e,None)
        with self.assertRaises(BoundaryError):self.ledger.reserve('P02')
    def test_reservation_exceeded_halts(self):
        e=self.ledger.reserve('P01');self.ledger.settle(e,{'prompt_tokens':14000,'completion_tokens':10})
        self.assertTrue(self.ledger.data['halted'])
    def test_budget_blocks_next_reservation(self):
        e=self.ledger.reserve('P01');e['estimated_rmb']='0.99'
        with self.assertRaises(BoundaryError):self.ledger.reserve('P02')
    def test_invalid_task_not_authorized(self):
        with self.assertRaises(BoundaryError):self.ledger.reserve('P03')
    def test_existing_ledger_cannot_reset(self):
        with self.assertRaises(BoundaryError):BatchLedger(self.ledger.path,TEMP)
    def test_reservation_is_durable_before_call(self):
        self.ledger.reserve('P01')
        data=json.loads(self.ledger.path.read_text(encoding='utf-8'))
        self.assertEqual(data['calls'][0]['status'],'reserved')
