"""Source catalog, evaluation snapshot and formal exact-ID read-only tools."""
import hashlib
import json
from pathlib import Path
from .planning_runtime import DATASET, BoundaryError, entity_id


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def iter_array(path):
    decoder = json.JSONDecoder()
    with Path(path).open(encoding='utf-8') as stream:
        buffer, position = stream.read(1024 * 1024), 0
        if not buffer.lstrip().startswith('['): raise BoundaryError('data_array')
        position = buffer.index('[') + 1
        while True:
            while position < len(buffer) and buffer[position] in ' \r\n\t,': position += 1
            if position < len(buffer) and buffer[position] == ']': return
            try:
                item, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                more = stream.read(1024 * 1024)
                if not more: raise BoundaryError('data_incomplete')
                buffer, position = buffer[position:] + more, 0
                continue
            yield item
            position = end


def location(proof):
    keys = ('file', 'file_sha256', 'line', 'record_id', 'disease', 'field', 'index')
    return {k: proof[k] for k in keys if k in proof}


class SourceCatalog:
    def __init__(self, data_root, expected_hash):
        self.data_root = Path(data_root)
        self.source_hash = sha_file(self.data_root / 'medical.json')
        if self.source_hash != expected_hash: raise BoundaryError('source_hash')
        with (self.data_root / 'medical.json').open(encoding='utf-8') as stream:
            self.records = {i: json.loads(line) for i, line in enumerate(stream, 1)}
        self.entities = {x['id']: x for x in iter_array(self.data_root / 'full_v1/entities.json') if x['type'] == 'Disease'}

    def check_entity(self, eid, name):
        if eid != entity_id(name) or eid not in self.entities or self.entities[eid]['name'] != name:
            raise BoundaryError('entity_identity')

    def check_proof(self, eid, name, field, value, source, target=None):
        self.check_entity(eid, name)
        raw_field = 'acompany' if field == 'acompany_with' else field
        if (not isinstance(source, dict) or source.get('file') != 'bishe/data/medical.json'
                or source.get('file_sha256') != self.source_hash or type(source.get('line')) is not int
                or source.get('field') != raw_field or source.get('disease', '').strip() != name):
            raise BoundaryError('source_location')
        raw = self.records.get(source['line'])
        if raw is None or raw['_id'] != source.get('record_id') or raw['name'].strip() != name:
            raise BoundaryError('source_record')
        if field == 'acompany_with':
            index = source.get('index')
            values = raw.get(raw_field, [])
            if type(index) is not int or not 0 <= index < len(values) or values[index].strip() != value:
                raise BoundaryError('relation_source')
            self.check_entity(target, value)
            return False
        elif field in ('desc', 'prevent'):
            if raw.get(raw_field, '')[:1800] != value: raise BoundaryError('text_source')
            return len(raw[raw_field]) > 1800
        else:
            raise BoundaryError('source_field')


class SnapshotTools:
    def __init__(self, catalog, seed_ids):
        self.catalog = catalog
        self.edges = {eid: [] for eid in seed_ids}
        for edge in iter_array(catalog.data_root / 'full_v1/relations.json'):
            if edge['type'] == 'acompany_with' and edge['start_id'] in self.edges:
                target = catalog.entities[edge['end_id']]
                self.edges[edge['start_id']].append({'id': target['id'], 'name': target['name'], 'source': location(edge['sources'][0])})
        self.edges = {eid: sorted(rows, key=lambda x: x['name'])[:8] for eid, rows in self.edges.items()}

    def read(self, action, timeout):
        if action['kind'] == 'read_relations':
            return {'dataset_id': DATASET, 'status': 'found', 'entities': self.edges[action['entity_id']]}
        eid = action['entity_ids'][0]
        entity = self.catalog.entities[eid]
        fields = {}
        for field in action['fields']:
            value = entity.get('properties', {}).get(field)
            source = entity.get('property_sources', {}).get(field)
            fields[field] = ({'status': 'found', 'value': value[:1800], 'source': location(source), 'truncated': len(value) > 1800}
                             if isinstance(value, str) and value.strip() and source else {'status': 'missing'})
        return {'dataset_id': DATASET, 'status': 'found', 'entity_id': eid, 'fields': fields}


ENTITY_QUERY = '''MATCH (d:CuratedEntity {dataset_id:$dataset,entity_type:'Disease',id:$id})
RETURN d.id AS id,d.name AS name,d.properties_json AS properties_json,
d.property_sources_json AS property_sources_json'''
EDGES_QUERY = '''MATCH (d:CuratedEntity {dataset_id:$dataset,id:$id})-[r:acompany_with]->
(n:CuratedEntity {dataset_id:$dataset,entity_type:'Disease'})
WHERE r.dataset_id=$dataset
RETURN n.id AS id,n.name AS name,r.sources_json AS sources_json ORDER BY n.name LIMIT 8'''


class ReadOnlyTools:
    def read(self, action, timeout):
        from django.conf import settings
        from .read_tools import bounded_read
        return bounded_read(settings.NEO4J_CONFIG, settings.NEO4J_DATABASE, self.transaction, (action,), timeout)

    @staticmethod
    def transaction(tx, action):
        from .graph_retrieval import MARKER_QUERY
        markers = list(tx.run(MARKER_QUERY, dataset=DATASET))
        if len(markers) != 1 or markers[0]['state'] != 'complete' or not markers[0]['plan_sha256']:
            raise BoundaryError('dataset_incomplete')
        eid = action['entity_id'] if action['kind'] == 'read_relations' else action['entity_ids'][0]
        entities = list(tx.run(ENTITY_QUERY, dataset=DATASET, id=eid))
        if len(entities) != 1: raise BoundaryError('entity_missing')
        if action['kind'] == 'read_relations':
            rows = [{'id': x['id'], 'name': x['name'], 'source': location(json.loads(x['sources_json'])[0])}
                    for x in tx.run(EDGES_QUERY, dataset=DATASET, id=eid)]
            return {'dataset_id': DATASET, 'status': 'found', 'entities': rows}
        entity = entities[0]
        properties, sources = json.loads(entity['properties_json']), json.loads(entity['property_sources_json'])
        fields = {}
        for field in action['fields']:
            value = properties.get(field)
            source = sources.get(field)
            if value and not source: raise BoundaryError('source_missing')
            fields[field] = ({'status': 'found', 'value': value[:1800], 'source': location(source), 'truncated': len(value) > 1800}
                             if isinstance(value, str) and value.strip() and source else {'status': 'missing'})
        return {'dataset_id': DATASET, 'status': 'found', 'entity_id': eid, 'fields': fields}
