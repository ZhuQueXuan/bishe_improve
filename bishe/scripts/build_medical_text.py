"""Offline source-checked text corpus. Never imports or modifies the graph."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from build_curated_data import PROJECT, SOURCE, digest, encode
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.text_chunks import chunks_for_record, MAX_CHARS

OUTPUT = PROJECT / 'data/medical_text_v1'


def build():
    source_hash = digest(SOURCE)
    raw = {line: json.loads(text) for line, text in enumerate(SOURCE.read_text(encoding='utf-8').splitlines(), 1)}
    retained_path = PROJECT / 'data/full_v1/source_records.json'
    retained = json.loads(retained_path.read_text(encoding='utf-8'))
    chunks, seen = [], set()
    for item in retained:
        proof, record = item['source'], item['raw_record']
        if proof['file_sha256'] != source_hash or raw.get(proof['line']) != record or record['_id'] != proof['record_id']:
            raise ValueError('Full dataset does not match original source')
        for chunk in chunks_for_record(record, proof):
            if chunk['id'] in seen:
                raise ValueError('Duplicate chunk identity')
            seen.add(chunk['id'])
            chunks.append(chunk)
    if digest(SOURCE) != source_hash:
        raise ValueError('Source changed during generation')
    stats = {'dataset_id': 'medical_text_v1', 'diseases': len(retained), 'chunks': len(chunks),
             'by_field': dict(sorted(Counter(c['field'] for c in chunks).items())),
             'max_characters': MAX_CHARS, 'overlap_characters': 0, 'source_sha256': source_hash,
             'retained_records_sha256': digest(retained_path), 'medical_review_status': 'not_verified',
             'source_validation': 'exact_original_record_and_span', 'database_writes': 0}
    return chunks, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not (output.is_relative_to(PROJECT / 'data') or output.is_relative_to(PROJECT.parent / 'temp')):
        parser.error('Output must be a new directory under project data or temp')
    chunks, stats = build()
    output.mkdir(parents=True)
    (output / 'chunks.json').write_bytes(encode(chunks))
    stats['chunks_sha256'] = digest(output / 'chunks.json')
    (output / 'statistics.json').write_bytes(encode(stats))
    (output / 'README.md').write_text(
        '# 可追溯医疗文本片段 v1\n\n'
        '从 full_v1 保留的疾病对应原始记录生成，逐条与 medical.json 核对。\n'
        '疾病描述、病因、预防、症状、检查、治疗六类；字符串按标点优先切成最多 600 字，'
        '列表逐项切分，不改字、不生成诊断、不补全缺失值。\n'
        'source.start/end 是原字符串中的 Python 字符位置，end 不含；列表另有 index。'
        '拼接同字段同列表项的片段可还原原文，空白内容不进入检索。\n'
        '工程出处检查通过；医学内容未审核。当前片段保存在磁盘，尚未逐片段导入 Neo4j 或生成向量。\n', encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
