"""Default: local config validation only. Paid smoke needs explicit batch approval."""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.embedding import EmbeddingClient, EmbeddingConfig, EmbeddingError, MODEL, build_payload

TEXTS = ['Synthetic fixture A: document about blood pressure monitoring.',
         'Synthetic fixture B: document about database administration.']
QUERY = ['Find the document about blood pressure monitoring.']
PRICE_PER_MILLION = Decimal('0.5')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-authorized', action='store_true')
    parser.add_argument('--approval-label')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        config = EmbeddingConfig.load()
    except EmbeddingError as error:
        print(json.dumps({'configured': False, 'reason': str(error), 'external_model_calls': 0}))
        return 1
    if not args.execute_authorized:
        print(json.dumps({'configured': True, 'model': MODEL, 'dimensions': 1024,
                          'config_fingerprint': config.fingerprint(), 'external_model_calls': 0}))
        return 0
    if not args.approval_label or len(args.approval_label) > 100:
        parser.error('Provide the label of a separately authorized two-request smoke batch.')
    if args.report is None:
        parser.error('A new report under project temp is required.')
    report_path = args.report.resolve()
    temp = (PROJECT.parent / 'temp').resolve()
    if not report_path.is_relative_to(temp) or report_path.exists():
        parser.error('Use a new report under project temp.')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation before requests prevents overwriting or reusing this batch report.
    report = {'batch_id': uuid.uuid4().hex, 'approval_label': args.approval_label,
              'started_at': datetime.now(timezone.utc).isoformat(), 'model': MODEL,
              'config_fingerprint': config.fingerprint(), 'max_requests': 2,
              'budget_yuan': '0.01', 'records': [], 'status': 'started',
              'scope': 'synthetic_connection_check_not_medical_retrieval_evaluation'}
    report_path.open('x', encoding='utf-8').close()

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    client = EmbeddingClient(config, allow_external=True)
    try:
        documents = None
        for texts, role in ((TEXTS, 'document'), (QUERY, 'query')):
            if len(client.records) >= 2:
                raise EmbeddingError('request_budget_exhausted')
            # Conservative byte-based allowance; not a provider-side billing hard cap.
            payload = build_payload(texts, role)
            allowance = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
            known = sum(row['total_tokens'] for row in client.records)
            if Decimal(known + allowance) * PRICE_PER_MILLION / 1000000 > Decimal('0.01'):
                raise EmbeddingError('estimated_budget_exhausted')
            report['status'] = 'request_pending'
            report['pending_role'] = role
            save()
            vectors = client.embed(texts, role)
            report['records'] = client.records
            report.pop('pending_role', None)
            save()
            if client.records[-1]['total_tokens'] is None:
                raise EmbeddingError('usage_unknown_stop')
            if role == 'document':
                documents = vectors
            else:
                scores = [sum(a*b for a, b in zip(vectors[0], vector)) for vector in documents]
                report['similarities'] = scores
                report['expected_fixture_ranked_first'] = scores[0] > scores[1]
        report['status'] = 'completed'
        report['usage_tokens'] = sum(row['total_tokens'] for row in client.records)
        report['estimated_cost_yuan'] = str(Decimal(report['usage_tokens']) * PRICE_PER_MILLION / 1000000)
        report['within_budget'] = Decimal(report['estimated_cost_yuan']) <= Decimal('0.01')
        report['success'] = report['within_budget'] and report['expected_fixture_ranked_first']
    except EmbeddingError as error:
        report.pop('pending_role', None)
        report.update(status='stopped', error=str(error), records=client.records, success=False)
    finally:
        save()
    print(json.dumps({'success': report.get('success', False), 'status': report['status'],
                      'external_model_calls': len(client.records), 'report': str(report_path)}, ensure_ascii=True))
    return 0 if report.get('success') else 1


if __name__ == '__main__': raise SystemExit(main())
