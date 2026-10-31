"""Validate frozen evaluation questions/anchors locally. No model or Docker calls."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.document_library import DocumentLibrary, digest

SUITE = PROJECT / 'data/rag_evaluation_v1/cases.json'


def require(condition, code):
    if not condition:
        raise ValueError(code)


def validate_schema(suite):
    require(suite.get('status') == 'prepared_not_run', 'unexpected_suite_status')
    require(suite.get('blind_test') is False, 'cannot_claim_blind_test')
    require(suite.get('case_count') == 20 and suite.get('mode_count') == 3, 'suite_size')
    cases, refs = suite['cases'], suite['references']
    require(len(cases) == 20, 'case_count')
    require([r['id'] for r in cases] == [f'Q{i:02}' for i in range(1, 21)], 'case_ids')
    require(len({r['query'] for r in cases}) == 20, 'duplicate_question')
    for row in cases:
        require(isinstance(row['query'], str) and 0 < len(row['query']) <= 1000, 'question_size')
        require(row['expected_behavior'] in {'answer', 'insufficient', 'out_of_scope', 'clarify'}, 'behavior')
        require(len(row['required_points']) >= 2, 'missing_judging_points')
        require(set(row['reference_ids']) <= set(refs), 'unknown_reference')
        if row['expected_behavior'] == 'answer':
            require(bool(row['reference_ids']), 'answer_without_reference')
        require(len({r['id'] for r in row['history']}) == len(row['history']), 'history_ids')
        require(all(r['role'] in {'user', 'assistant'} and r['content'] for r in row['history']), 'history_schema')
        require(row['familiarity'] in {'new_wording_known_corpus', 'rephrased_previous_development_topic',
                                      'rephrased_previous_nonmedical_acceptance'}, 'familiarity_label')
    require(Counter(r['category'] for r in cases) ==
            {'document':8, 'graph':2, 'source_conflict':2, 'boundary':4, 'session':4}, 'case_balance')
    policy = suite['run_policy']
    require(policy['enabled'] is False and policy['external_calls_this_preparation'] == 0, 'live_execution_not_authorized_by_file')
    require(policy['retries'] == 0, 'retry_policy')
    require(set(policy['initial_pilot_case_ids']) <= {r['id'] for r in cases}, 'pilot_ids')


def revision_snapshot(suite, path):
    raw = path.read_bytes()
    require(digest(raw) == path.with_suffix('.sha256').read_text(encoding='ascii').strip(), 'revision_checksum_mismatch')
    manifest = json.loads(raw)
    require(manifest['base_suite_sha256'] == digest(SUITE.read_bytes()), 'revision_base_suite_mismatch')
    base = suite['snapshot']['code_files']
    extra = {'bishe/knowledge_graph_backend/api/answer_boundaries.py'}
    changed = {'bishe/knowledge_graph_backend/api/chat_runtime.py','bishe/knowledge_graph_backend/api/utils.py'}
    current = manifest['code_files']
    require(set(current) == set(base) | extra, 'revision_code_file_set')
    require(manifest['base_code_files'] == base, 'revision_base_code_mismatch')
    require(all(current[k] == base[k] for k in set(base)-changed), 'revision_unexpected_code_change')
    require(manifest['purpose']=='Q18_missing_notice_and_Q19_explicit_plural_fix', 'revision_scope')
    return manifest


def audit(path=SUITE, *, revision_path=None):
    raw = path.read_bytes()
    require(digest(raw) == path.with_suffix('.sha256').read_text(encoding='ascii').strip(), 'frozen_suite_checksum_mismatch')
    suite = json.loads(raw)
    validate_schema(suite)
    workspace = PROJECT.parent
    revision = revision_snapshot(suite, revision_path) if revision_path else None
    expected_code = revision['code_files'] if revision else suite['snapshot']['code_files']
    for rel, expected in expected_code.items():
        require(digest((workspace / rel).read_bytes()) == expected, 'code_snapshot_mismatch:' + rel)
    source = PROJECT / 'data/medical.json'
    require(digest(source.read_bytes()) == suite['snapshot']['medical_json_sha256'], 'graph_snapshot_mismatch')
    records = {i:json.loads(line) for i, line in enumerate(source.open(encoding='utf-8'), 1)}
    library = DocumentLibrary()
    actual = {r['id']:r for r in library.versions() if r['active']}
    require(set(actual) == {r['version_id'] for r in suite['snapshot']['documents']}, 'active_document_snapshot_mismatch')
    for row in suite['snapshot']['documents']:
        v = actual[row['version_id']]
        require(v['file_hash'] == row['sha256'] and v['chunks'] == row['chunks'] and
                v['metadata']['selected_pages'] == row['selected_pages'], 'document_snapshot_mismatch')
        library.verify(v['id'])
    for ref in suite['references'].values():
        if ref['kind'] == 'document':
            e = library.evidence(ref['chunk_id'], list(actual))
            for a, b in [('text','quote'), ('version_id','version_id'), ('page','page'), ('doc_id','doc_id'),
                         ('text_sha256','text_sha256'), ('file_sha256','file_sha256'), ('start','start'), ('end','end')]:
                require(e[a] == ref[b], 'document_reference_mismatch')
            normalized = lambda s: ''.join(s.split())
            require(normalized(ref['focus_text']) in normalized(ref['quote']), 'document_focus_missing')
        else:
            require(ref['kind'] == 'graph' and ref['file_sha256'] == digest(source.read_bytes()), 'graph_reference_hash')
            row = records[ref['line']]
            require(row['name'] == ref['disease'] and ref['focus_text'] in row[ref['field']], 'graph_reference_mismatch')
    # Detect exact leakage against earlier dev/live questions. Semantic overlap is labelled manually;
    # no exact duplicate does NOT imply an independent test.
    docroot = PROJECT / 'data/document_library_v1'
    earlier = [r['query'] for r in json.loads((docroot/'retrieval_dev_cases.json').read_text(encoding='utf-8'))]
    earlier += [r['query'] for r in json.loads((docroot/'admission_review_20261023.json').read_text(encoding='utf-8'))['cases']]
    earlier += ['如何看食品标签？','糖尿病患者饮食应该注意什么？','肺癌药品有哪些厂家和品牌？',
                '帮我写一个Python排序程序','如何预防跌倒？']
    require(not {r['query'] for r in suite['cases']} & set(earlier), 'exact_old_question_duplicate')
    return {'suite_id':suite['suite_id'],'status':'frozen_suite_validated_not_model_results','cases':20,'planned_mode_runs':60,
            'implementation_revision':revision['revision_id'] if revision else 'baseline',
            'reference_anchors_verified':len(suite['references']),'frozen_sha256':digest(raw),
            'case_categories':dict(Counter(r['category'] for r in suite['cases'])),
            'no_exact_old_questions':True,'independent_blind_test':False,
            'source_snapshot_verified':True,'external_calls':0,'database_writes':0,
            'results':'not_run_no_quality_scores'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Optional JSON report, must stay under this project temp directory')
    parser.add_argument('--revision-manifest', type=Path, help='Explicit hash-pinned repair implementation; original question file remains frozen')
    args = parser.parse_args()
    if args.report:
        require(args.report.resolve().is_relative_to((PROJECT.parent/'temp').resolve()), 'report_must_be_project_temp')
    result = audit(revision_path=args.revision_manifest)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
