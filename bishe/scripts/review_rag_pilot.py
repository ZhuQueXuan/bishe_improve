"""Local evidence audit and empty human score forms; never invokes a judge/model."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'bishe/knowledge_graph_backend'))
DEFAULT=ROOT/'temp/rag_evaluation_v1/pilot_20261023/results.json'


def norm(value): return re.sub(r'\s+','',value)


def review(path=DEFAULT):
    report=json.loads(path.read_text(encoding='utf-8'))
    suite=json.loads((ROOT/'bishe/data/rag_evaluation_v1/cases.json').read_text(encoding='utf-8'))
    cases={c['id']:c for c in suite['cases']}
    ledger=json.loads(path.with_name('ledger.json').read_text(encoding='utf-8'))
    if report['status']=='running':
        raise ValueError('batch_still_running')
    rows=[]
    from api.document_library import DocumentLibrary
    library=DocumentLibrary()
    versions=[r['version_id'] for r in suite['snapshot']['documents']]
    graph_hash=hashlib.sha256((ROOT/'bishe/data/medical.json').read_bytes()).hexdigest()
    records={n:json.loads(line) for n,line in enumerate((ROOT/'bishe/data/medical.json').open(encoding='utf-8'),1)}
    notes=['# 首轮小批次：回答与实际证据核对','',
           '这是模型实际输出及自动机械核验。以下没有自动生成质量分，合法E编号不等于内容正确；尚需逐条审阅。','']
    template=json.loads((ROOT/'bishe/data/rag_evaluation_v1/review_template.json').read_text(encoding='utf-8'))
    indexed={(r['case_id'],r['mode']):r for r in template['results']}
    for row in report['results']:
        r=row.get('retrieval') or {}
        facts=r.get('facts',[]) if r.get('context') else []
        ids={f['evidence_id'] for f in facts}
        cited=set(re.findall(r'\bE\d+\b',row.get('answer') or ''))
        calls=[c for c in ledger['calls'] if c['run_id']==row['run_id']]
        docfacts=[f for f in facts if f.get('kind')=='document']
        source_checks=[]
        for fact in facts:
            proof=fact['sources'][0]
            if proof.get('kind')=='document':
                actual=library.evidence(proof['chunk_id'],versions)
                valid=(fact['value']==actual['text'] and all(proof[k]==actual[k] for k in
                       ['version_id','doc_id','page','file_sha256','text_sha256','start','end']))
            else:
                original=records[proof['line']]
                raw=original[proof['field']]
                value=norm(fact['value'])
                valid=(proof['file_sha256']==graph_hash and original['name']==fact['disease'] and
                       (norm(raw).startswith(value) if isinstance(raw,str) else value in [norm(x) for x in raw]))
            source_checks.append({'evidence_id':fact['evidence_id'],'original_matches':valid})
        embedding_input_checks=[c['input_sha256']==hashlib.sha256(row['resolved_answer_question'].encode()).hexdigest()
                                for c in calls if c['kind']=='embedding']
        mode_violation=(row['mode']=='G' and (bool(docfacts) or any(c['kind']=='embedding' for c in calls))) or (
            row['mode']=='D' and (any(f.get('kind')!='document' for f in facts) or any(c['kind']=='understand' for c in calls)
                                or row.get('agent',{}).get('counts',{}).get('retrieval',0)>0))
        focus_checks=[]
        for ref_id in cases[row['case_id']]['reference_ids']:
            ref=suite['references'][ref_id]
            hits=[]
            for fact in facts:
                source=fact['sources'][0]
                same=(source.get('version_id')==ref.get('version_id') if ref['kind']=='document' else
                      fact.get('disease')==ref['disease'] and source.get('field')==ref['field'])
                if same and norm(ref['focus_text']) in norm(fact['value']): hits.append(fact['evidence_id'])
            focus_checks.append({'reference_id':ref_id,'focus_text_seen':bool(hits),'actual_evidence_ids':hits,
                                 'meaning':'literal anchor coverage only; alternatives require review'})
        audit={'run_id':row['run_id'],'status':row['status'],'source_mode_violation':bool(mode_violation),
               'known_evidence_ids':sorted(ids),'cited_ids':sorted(cited),'unknown_cited_ids':sorted(cited-ids),
               'all_evidence_cited':ids<=cited,'has_substantive_context':bool(r.get('context')),
               'document_passages':len(docfacts),'graph_facts':len(facts)-len(docfacts),
               'sources_match_originals':all(x['original_matches'] for x in source_checks),
               'source_checks':source_checks,'embedding_only_resolved_question':all(embedding_input_checks),
               'reference_literal_checks':focus_checks,'usage_complete':all(c['status']=='settled' for c in calls),
               'call_counts':dict(Counter(c['kind'] for c in calls)),
               'latency_ms':row['latency_ms'],'estimated_rmb':row['estimated_rmb']}
        rows.append(audit)
        dest=indexed[(row['case_id'],row['mode'])]
        dest.update(status=row['status'],batch_id=report['plan']['batch_id'],attempt_number=1,
            technical_error=row['technical_error'],answer=row['answer'],actual_evidence=facts,
            retrieved_document_candidates=docfacts,latency_ms=row['latency_ms'],calls=row['calls'],
            provider_usage=row['provider_usage'],estimated_cost_rmb=row['estimated_rmb'],
            cost_basis=ledger['cost_basis'],usage_complete=audit['usage_complete'])
        notes.extend([f"## {row['run_id']}",'',f"问题：{row['query']}",
            f"状态：{row['status']}；图谱事实{len(facts)-len(docfacts)}，文档片段{len(docfacts)}；耗时{row['latency_ms']/1000:.2f}秒。",'',
            '### 实际回答','',row['answer'] or '（没有接受最终回答；查看账本中的原输出及拒绝原因）','',
            '### 实际证据',''])
        for fact in facts:
            s=fact['sources'][0]
            location=(f"{s['title']}，{s['edition']}，PDF第{s['page']}页" if s.get('kind')=='document' else
                      f"medical.json第{s['line']}行，{fact['disease']}/{fact['field']}")
            notes.extend([f"- [{fact['evidence_id']}] {location}",'',fact['value'],''])
        notes.extend(['自动锚点检查：'+json.dumps(focus_checks,ensure_ascii=False),''])
    result={'batch_id':report['plan']['batch_id'],'status':report['status'],
            'attempted':len(rows),'expected':report['plan']['planned_runs'],'external_calls':len(ledger['calls']),
            'source_mode_violations':sum(r['source_mode_violation'] for r in rows),
            'source_mismatch_runs':sum(not r['sources_match_originals'] for r in rows),
            'embedding_input_violations':sum(not r['embedding_only_resolved_question'] for r in rows),
            'unknown_citation_runs':sum(bool(r['unknown_cited_ids']) for r in rows),
            'usage_complete':all(r['usage_complete'] for r in rows),
            'protected_unchanged':report['protected_unchanged'],'quality_scores':'not_automatically_assigned','runs':rows}
    output=path.parent
    (output/'mechanical_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'evidence_review.md').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    template.update(status='pilot_recorded_human_review_pending',batch_id=report['plan']['batch_id'])
    (output/'human_review.json').write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='runs'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    review(Path(sys.argv[1]) if len(sys.argv)>1 else DEFAULT)
