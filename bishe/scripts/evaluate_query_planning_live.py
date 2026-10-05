"""Explicitly approved, bounded current-question-only live retrieval comparison."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from build_curated_data import PROJECT, SOURCE, encode, digest

sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
CASES = PROJECT / 'data/query_planning_live_cases_v1.json'
OUTPUT = PROJECT / 'data/query_planning_live_results_20261004.json'
JOURNAL = PROJECT.parent / 'temp/query-planning-live-journal_20261004.json'
# Budget guard uses peak prices and a conservative conversion, not a currency quote.
PEAK = {'input': .3, 'cached_input': .006, 'output': 1.2}
OFF_PEAK = {'input': .15, 'cached_input': .003, 'output': .6}
CNY_PER_USD_BUDGET = 8


def worst_cost(messages):
    input_bound = sum(len(m['content'].encode('utf-8')) for m in messages) + 256
    return (input_bound * PEAK['input'] + 600 * PEAK['output']) / 1_000_000


class Recorder:
    def __init__(self, client, allowed_questions):
        self.client = client
        self.allowed = set(allowed_questions)
        self.calls = []
        self.reserved = 0
        self.halted = False

    def save(self):
        JOURNAL.write_bytes(encode({'calls': self.calls, 'reserved_usd_upper_bound': self.reserved,
                                   'budget_cny': 1, 'model': 'deepseek-flash'}))

    def with_options(self, **options):
        if options != {'max_retries': 0}:
            raise ValueError('Unexpected SDK retries')
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))

    def create(self, **kwargs):
        from api.query_planning import SYSTEM_PROMPT
        from urllib.parse import urlsplit
        messages = kwargs['messages']
        if (self.halted or len(self.calls) >= 12 or kwargs.get('model') != 'deepseek-flash'
                or kwargs.get('max_tokens') != 600 or kwargs.get('timeout') != 15
                or kwargs.get('stream') is not False
                or urlsplit(str(self.client.base_url)).hostname != 'api.deepseek.com'
                or len(messages) != 2 or messages[0] != {'role':'system','content':SYSTEM_PROMPT}
                or messages[1]['role'] != 'user'):
            raise ValueError('Approved call boundary exceeded')
        payload = json.loads(messages[1]['content'])
        if set(payload) != {'question'} or payload['question'] not in self.allowed:
            raise ValueError('Unapproved content')
        reserve = worst_cost(messages)
        if (self.reserved + reserve) * CNY_PER_USD_BUDGET > 1:
            self.halted = True
            raise ValueError('Budget exhausted')
        self.reserved += reserve
        entry = {'question':payload['question'], 'status':'started', 'reserved_usd':reserve}
        self.calls.append(entry)
        self.save()  # Durable record before any paid request; no automatic retries/resume.
        started = time.perf_counter()
        try:
            response = self.client.with_options(max_retries=0).chat.completions.create(**kwargs)
            usage = response.usage
            if usage is None:
                self.halted = True
                entry['status'] = 'usage_missing'
            else:
                prompt, completion = usage.prompt_tokens, usage.completion_tokens
                hit = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
                miss = getattr(usage, 'prompt_cache_miss_tokens', prompt-hit)
                entry.update(status='returned', response_model=response.model, usage={
                    'prompt_tokens':prompt,'completion_tokens':completion,
                    'cache_hit_tokens':hit,'cache_miss_tokens':miss},
                    estimated_usd_off_peak=(miss*OFF_PEAK['input']+hit*OFF_PEAK['cached_input']+completion*OFF_PEAK['output'])/1_000_000)
                if prompt * PEAK['input']/1_000_000 + completion * PEAK['output']/1_000_000 > reserve:
                    self.halted = True
            entry['finish_reason'] = response.choices[0].finish_reason if response.choices else None
            return response
        except Exception as error:
            entry.update(status='failed', error_type=type(error).__name__)
            # Service/configuration errors stop further paid attempts; timeout has no retry.
            if type(error).__name__ not in {'APITimeoutError'}:
                self.halted = True
            raise
        finally:
            entry['latency_ms'] = round((time.perf_counter()-started)*1000, 2)
            self.save()


def grade(case, result):
    if 'status' in case:
        return result['status'] == case['status'] and not result['facts']
    return (result['status'] == 'found' and set(result.get('diseases', [])) == set(case['diseases'])
            and {f['field'] for f in result['facts']} == set(case['fields']))


def validate_facts(result, raw, source_hash):
    count = 0
    for fact in result['facts']:
        proof = fact['sources'][0]
        record = raw[proof['line']]
        value = record[proof['field']]
        if 'index' in proof:
            value = value[proof['index']]
        if 'start' in proof:
            value = value[proof['start']:proof['end']]
        elif fact['field'] in {'desc','cause','prevent'}:
            value = value[:1800]
        else:
            value = value.strip()
        if (value != fact['value'] or record['name'].strip() != fact['disease']
                or record['_id'] != proof['record_id'] or proof['file_sha256'] != source_hash
                or fact['medical_review_status'] != 'not_verified'):
            raise ValueError('Evidence source conflict')
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-approved-12', action='store_true')
    args = parser.parse_args()
    cases = json.loads(CASES.read_text(encoding='utf-8'))['cases']
    if not args.run_approved_12:
        print(json.dumps({'mode':'offline_preview','cases':len(cases),'max_calls':12,'budget_cny':1,
                          'model':'deepseek-flash','case_file_sha256':digest(CASES)}))
        return
    if len(cases) != 12 or OUTPUT.exists() or JOURNAL.exists():
        raise ValueError('Fresh single approved run required; do not repeat paid test')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
    import django
    django.setup()
    from api.utils import DeepSeekAPI
    from api.graph_retrieval import retrieve_context, graph_statistics
    from api.fulltext_retrieval import retrieve_phrase
    from api.query_planning import planned_retrieval
    model = DeepSeekAPI()
    available = [m.id for m in model.client.with_options(max_retries=0, timeout=15).models.list().data]
    if 'deepseek-flash' not in available:
        raise ValueError('Approved test model unavailable')
    original_model = model.model
    model.model = 'deepseek-flash'  # Only this test instance; never settings or .env.
    recorder = Recorder(model.client, [c['query'] for c in cases])
    model.client = recorder
    before = graph_statistics()
    source_hash = digest(SOURCE)
    raw = {line:json.loads(text) for line,text in enumerate(SOURCE.read_text(encoding='utf-8').splitlines(),1)}
    rows = []
    checked = 0
    for case in cases:
        start = time.perf_counter()
        baseline = retrieve_context(case['query'], case.get('history', []))
        baseline_ms = round((time.perf_counter()-start)*1000, 2)
        start = time.perf_counter()
        result = planned_retrieval(case['query'], case.get('history', []), baseline, model, retrieve_context, retrieve_phrase)
        extra_ms = round((time.perf_counter()-start)*1000, 2)
        checked += validate_facts(baseline, raw, source_hash) + validate_facts(result, raw, source_hash)
        rows.append({'id':case['id'],'query':case['query'],'category':case['category'],
                     'old_passed':grade(case,baseline),'new_passed':grade(case,result),
                     'old_latency_ms':baseline_ms,'additional_latency_ms':extra_ms,
                     'old':baseline,'new':result})
        print(json.dumps({'case':case['id'],'old_passed':rows[-1]['old_passed'],
                          'new_passed':rows[-1]['new_passed'],'calls':len(recorder.calls)}),flush=True)
    unchanged = graph_statistics() == before and digest(SOURCE) == source_hash
    if not unchanged:
        raise ValueError('Graph statistics or raw source changed')
    report = {'status':'completed','scope':'approved_12_current_question_only_no_answer_generation',
              'configured_model_unchanged':original_model,'test_model':'deepseek-flash',
              'case_sha256':digest(CASES),'source_sha256':source_hash,'cases':rows,'calls':recorder.calls,
              'summary':{'cases':12,'old_passed':sum(r['old_passed'] for r in rows),
                         'new_passed':sum(r['new_passed'] for r in rows),'paid_request_attempts':len(recorder.calls),
                         'source_facts_verified':checked,'graph_unchanged':unchanged,'database_writes':0,
                         'mean_added_latency_ms':round(sum(r['additional_latency_ms'] for r in rows)/12,2),
                         'cost_reserved_upper_bound_cny':recorder.reserved*CNY_PER_USD_BUDGET,
                         'estimated_usage_cost_usd':sum(c.get('estimated_usd_off_peak',0) for c in recorder.calls)},
              'pricing_source':'https://api-docs.deepseek.com/quick_start/pricing/',
              'pricing':{'off_peak':OFF_PEAK,'guard_peak':PEAK,'usd_to_cny_budget_buffer':8},
              'limitations':['Not medical accuracy; single small selected retrieval comparison.',
                             'Reported model usage cost is calculated, not a verified billing deduction.',
                             'Failed calls with unknown usage remain budget-reserved.',
                             'Production model configuration unchanged; live test used approved temporary model name.']}
    OUTPUT.write_bytes(encode(report))
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
