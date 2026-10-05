"""Offline plan by default; explicit, audited creation of one reversible index only."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from build_curated_data import PROJECT, digest, encode

sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.fulltext_retrieval import INDEX, CREATE_INDEX, INDEX_STATUS, valid_schema, retrieve_phrase
from api.graph_retrieval import graph_statistics


def plan(uri, database):
    binding = {'index': INDEX, 'target_uri': uri, 'target_database': database,
               'ddl': CREATE_INDEX, 'source_sha256': digest(PROJECT / 'data/medical.json'),
               'tool_sha256': digest(Path(__file__)),
               'retriever_sha256': digest(PROJECT / 'knowledge_graph_backend/api/fulltext_retrieval.py'),
               'node_writes': 0, 'relationship_writes': 0, 'deletions': 0}
    return {**binding, 'plan_sha256': hashlib.sha256(encode(binding)).hexdigest()}


def create(driver, database, expected, audit):
    if not audit.is_relative_to(PROJECT / 'data') or audit.exists():
        raise ValueError('Audit must be a new directory under project data')
    with driver.session(database=database, default_access_mode='READ') as session:
        before = [dict(r) for r in session.run('SHOW INDEXES YIELD name,type,entityType,labelsOrTypes,properties,options RETURN *')]
        existing = [dict(r) for r in session.run(INDEX_STATUS, name=INDEX)]
    if existing and (len(existing) != 1 or not valid_schema(existing[0])):
        raise ValueError('Existing index conflicts; no schema changed')
    stats_before = graph_statistics()
    audit.mkdir(parents=True)
    (audit / 'plan.json').write_bytes(encode(expected))
    (audit / 'schema_before.json').write_bytes(encode(before))
    (audit / 'graph_before.json').write_bytes(encode(stats_before))
    (audit / 'ROLLBACK.md').write_text(
        '# 本次索引变更的恢复说明\n\n'
        + ('此前同名索引已存在，本次不要删除。\n' if existing else
           '本次只增加 medical_fulltext_v1 全文索引，未修改节点、关系、原文和聊天记录。\n'
           '恢复此前状态：在明确连接本次 plan.json 的目标数据库后，仅执行 '
           '`DROP INDEX medical_fulltext_v1 IF EXISTS`。重新建立使用本目录计划和正式管理程序。\n')
        + 'schema_before.json 和 graph_before.json 记录变更前状态；此目录不是数据库全量备份。\n', encoding='utf-8')
    with driver.session(database=database) as session:
        session.run(CREATE_INDEX).consume()
        session.run('CALL db.awaitIndex($name,60)', name=INDEX).consume()
        after = [dict(r) for r in session.run(INDEX_STATUS, name=INDEX)]
    if len(after) != 1 or after[0]['state'] != 'ONLINE' or not valid_schema(after[0]):
        raise ValueError('Index not ready; inspect saved audit and rollback instructions')
    stats_after = graph_statistics()
    if stats_before != stats_after:
        raise ValueError('Business graph statistics changed during index creation')
    result = {'status': 'passed', 'created': not bool(existing), 'index': after[0],
              'graph_unchanged': True, 'database_data_writes': 0, 'schema_write': not bool(existing)}
    (audit / 'result.json').write_bytes(encode(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--create-index', action='store_true')
    mode.add_argument('--read-only', action='store_true')
    parser.add_argument('--uri', default='bolt://127.0.0.1:7687')
    parser.add_argument('--database', default='neo4j')
    parser.add_argument('--confirm-plan')
    parser.add_argument('--audit', type=Path)
    parser.add_argument('--phrase')
    args = parser.parse_args()
    expected = plan(args.uri, args.database)
    if not args.create_index and not args.read_only:
        print(json.dumps(expected, ensure_ascii=False, indent=2))
        return
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
    import django
    django.setup()
    from django.conf import settings
    from neo4j import GraphDatabase
    if settings.NEO4J_CONFIG['HOST'] != args.uri or settings.NEO4J_DATABASE != args.database:
        parser.error('Explicit target does not match configured current database')
    if args.read_only:
        if not args.phrase:
            parser.error('Read-only mode requires --phrase')
        print(json.dumps(retrieve_phrase(args.phrase), ensure_ascii=False, indent=2))
        return
    if args.confirm_plan != expected['plan_sha256'] or not args.audit:
        parser.error('Creation requires exact --confirm-plan and new --audit directory')
    with GraphDatabase.driver(args.uri, auth=settings.NEO4J_CONFIG['AUTH'], connection_timeout=10) as driver:
        result = create(driver, args.database, expected, args.audit.resolve())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
