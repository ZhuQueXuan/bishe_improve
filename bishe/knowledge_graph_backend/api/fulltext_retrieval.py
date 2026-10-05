"""Read-only literal phrase tool for the first full-text milestone, not a diagnosis route."""
from .graph_retrieval import DATASET, MARKER_QUERY, RetrievalUnavailable, read_json, source_location, TEXT_FIELDS

INDEX = 'medical_fulltext_v1'
CREATE_INDEX = """CREATE FULLTEXT INDEX medical_fulltext_v1 IF NOT EXISTS
FOR (n:CuratedEntity) ON EACH [n.name, n.properties_json]
OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk', `fulltext.eventually_consistent`: false}}"""
INDEX_STATUS = 'SHOW FULLTEXT INDEXES YIELD name,state,labelsOrTypes,properties,options WHERE name=$name RETURN *'
SEARCH = '''CALL db.index.fulltext.queryNodes($index,$phrase) YIELD node,score
WHERE node.dataset_id=$dataset AND node.entity_type='Disease'
RETURN node.name AS name,node.properties_json AS properties_json,
 node.property_sources_json AS property_sources_json,score
ORDER BY score DESC,node.name LIMIT 100'''


def valid_schema(row):
    config = row.get('options', {}).get('indexConfig', {})
    return (row.get('labelsOrTypes') == ['CuratedEntity']
            and row.get('properties') == ['name', 'properties_json']
            and config.get('fulltext.analyzer') == 'cjk'
            and config.get('fulltext.eventually_consistent') is False)


def literal_query(phrase):
    if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 200:
        raise ValueError('检索短语须为 1～200 字的字符串')
    # User input is always one quoted literal; it cannot select Lucene fields or operators.
    return 'properties_json:"' + phrase.replace('\\', '\\\\').replace('"', '\\"') + '"'


def retrieve_transaction(tx, phrase, limit=5):
    lucene = literal_query(phrase)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('结果数量须为 1～10')
    markers = list(tx.run(MARKER_QUERY, dataset=DATASET))
    if len(markers) != 1 or markers[0]['state'] != 'complete' or not markers[0]['plan_sha256']:
        raise RetrievalUnavailable('完整图谱尚未准备好')
    indexes = [dict(row) for row in tx.run(INDEX_STATUS, name=INDEX)]
    if len(indexes) != 1 or indexes[0]['state'] != 'ONLINE' or not valid_schema(indexes[0]):
        raise RetrievalUnavailable('全文索引尚未准备好或配置不一致')
    facts = []
    rows = list(tx.run(SEARCH, index=INDEX, phrase=lucene, dataset=DATASET))
    for row in rows:
        props, proofs = read_json(row['properties_json'], dict), read_json(row['property_sources_json'], dict)
        for field, label in TEXT_FIELDS.items():
            text = props.get(field)
            if not isinstance(text, str) or phrase not in text:
                continue
            proof = proofs.get(field)
            source = source_location(proof)
            if proof.get('raw_value') != text or source.get('disease', '').strip() != row['name'] or source['field'] != field:
                raise RetrievalUnavailable('全文内容与出处不一致')
            # A bounded window centered on an exact occurrence, including cross-chunk matches.
            position = text.index(phrase)
            start = max(0, position - (600 - len(phrase)) // 2)
            end = min(len(text), start + 600)
            source.update(start=start, end=end)
            facts.append({'disease': row['name'], 'field': field, 'label': label,
                          'value': text[start:end], 'sources': [source], 'score': row['score'],
                          'retrieval_channel': 'fulltext', 'medical_review_status': 'not_verified'})
    kept = facts[:limit]
    return {'status': 'found' if kept else 'no_evidence', 'tool': 'fulltext_literal_phrase_v1',
            'dataset_id': DATASET, 'query': phrase, 'facts': kept,
            'candidate_diseases': len(rows), 'truncated': len(facts) > limit,
            'candidate_limit_reached': len(rows) == 100,
            'scope': ['desc', 'cause', 'prevent'], 'external_model_calls': 0}


def retrieve_phrase(phrase, limit=5, timeout=30):
    from django.conf import settings
    from .read_tools import bounded_read
    literal_query(phrase)
    try:
        return bounded_read(settings.NEO4J_CONFIG, settings.NEO4J_DATABASE,
                            retrieve_transaction, (phrase, limit), timeout)
    except (RetrievalUnavailable, ValueError):
        raise
    except Exception as error:
        raise RetrievalUnavailable('全文检索暂时不可用') from error
