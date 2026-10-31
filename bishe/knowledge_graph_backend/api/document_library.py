"""Local, versioned source registry. No Django, network or model calls.

PDF page numbers are physical (one based). Offsets refer to the saved extracted
page text, not PDF glyph coordinates. Source text is evidence, never instructions.
"""
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / 'data/document_library_v1/library'
PARSER_CONTRACT = 'pypdf-6.10.0-page-text-v1/chars600-overlap80-v1'
MAX_BYTES = 20 * 1024 * 1024


class LibraryError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc).isoformat()


def metadata_checked(metadata):
    fields = {'doc_id', 'title', 'edition', 'publisher', 'source_url', 'download_url',
              'published_date', 'effective_date', 'acquired_at', 'rights', 'scope_note',
              'selected_pages', 'printed_page_offset', 'sha256', 'page_text_end_before'}
    if not isinstance(metadata, dict) or set(metadata) - fields:
        raise LibraryError('invalid_metadata_fields')
    result = dict(metadata)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}', result.get('doc_id', '')):
        raise LibraryError('invalid_document_id')
    for field in ('title', 'edition', 'publisher', 'rights', 'scope_note', 'acquired_at'):
        value = result.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise LibraryError('missing_or_invalid_' + field)
    for field in ('source_url', 'download_url'):
        value = result.get(field)
        if not isinstance(value, str) or len(value) > 2048:
            raise LibraryError('invalid_source_url')
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise LibraryError('https_source_required')
    for field in ('published_date', 'effective_date'):
        value = result.get(field)
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (TypeError, ValueError):
                raise LibraryError('invalid_date') from None
    pages = result.get('selected_pages')
    if (not isinstance(pages, list) or not pages or len(pages) > 500
            or any(type(n) is not int or n < 1 for n in pages)
            or pages != sorted(set(pages))):
        raise LibraryError('explicit_sorted_page_selection_required')
    if type(result.get('printed_page_offset')) is not int:
        raise LibraryError('printed_page_offset_required')
    cuts = result.get('page_text_end_before', {})
    if (not isinstance(cuts,dict) or any(k not in {str(n) for n in pages}
            or not isinstance(v,str) or not 1 <= len(v) <= 200 for k,v in cuts.items())):
        raise LibraryError('invalid_page_exclusions')
    if not re.fullmatch(r'[0-9a-f]{64}', result.get('sha256', '')):
        raise LibraryError('expected_sha256_required')
    return result


def page_spans(text, size=600, overlap=80):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind(mark, start + size // 2, end) for mark in '。；！？\n')
            if boundary >= 0:
                end = boundary + 1
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def inspect_pdf(path, metadata):
    from io import BytesIO
    import pypdf
    from pypdf import PdfReader
    meta = metadata_checked(metadata)
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise LibraryError('file_too_large')
    data = path.read_bytes()
    if len(data) > MAX_BYTES or not data.startswith(b'%PDF-'):
        raise LibraryError('pdf_required')
    if digest(data) != meta['sha256']:
        raise LibraryError('source_hash_mismatch')
    if pypdf.__version__ != '6.10.0':
        raise LibraryError('parser_version_mismatch')
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise LibraryError('encrypted_pdf_unsupported')
        if not 1 <= len(reader.pages) <= 500 or max(meta['selected_pages']) > len(reader.pages):
            raise LibraryError('invalid_page_count_or_selection')
        # Keep all page extractions for audit, index only the approved page range.
        pages = [page.extract_text() or '' for page in reader.pages]
    except LibraryError:
        raise
    except Exception:
        raise LibraryError('pdf_extraction_failed') from None
    if sum(len(x) for x in pages) > 2_000_000:
        raise LibraryError('extracted_text_limit')
    for n in meta['selected_pages']:
        text = pages[n-1]
        if len(text.strip()) < 40 or text.count('\ufffd') > max(2, len(text) // 100):
            raise LibraryError('selected_page_requires_manual_extraction_review')
        marker = meta.get('page_text_end_before',{}).get(str(n))
        if marker and (text.count(marker) != 1 or text.index(marker) < 40):
            raise LibraryError('exclusion_marker_not_unique_or_invalid')
    identity_meta = {k: v for k, v in meta.items() if k != 'acquired_at'}
    version_id = digest(canonical({'metadata': identity_meta, 'parser': PARSER_CONTRACT}).encode())
    chunks = []
    for n in meta['selected_pages']:
        page_text = pages[n-1]
        marker = meta.get('page_text_end_before',{}).get(str(n))
        selected_text = page_text[:page_text.index(marker)] if marker else page_text
        for start, end, text in page_spans(selected_text):
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'medical-doc:{version_id}:{n}:{start}:{end}'))
            chunks.append({'id': chunk_id, 'page': n, 'printed_page': n + meta['printed_page_offset'],
                           'start': start, 'end': end, 'text': text,
                           'text_sha256': digest(text.encode())})
    if not chunks:
        raise LibraryError('no_chunks')
    return {'metadata': meta, 'version_id': version_id, 'pages': pages,
            'chunks': chunks, 'data': data}


class DocumentLibrary:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root).resolve()
        self.database = self.root / 'catalog.sqlite3'
        self._verified_sources = {}

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'objects').mkdir(exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY, active_version TEXT REFERENCES versions(id));
                CREATE TABLE IF NOT EXISTS versions(
                    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(id),
                    file_hash TEXT NOT NULL, metadata TEXT NOT NULL, pages TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
                    page INTEGER NOT NULL, printed_page INTEGER NOT NULL,
                    start INTEGER NOT NULL, end INTEGER NOT NULL,
                    text TEXT NOT NULL, text_sha256 TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id);
                CREATE TABLE IF NOT EXISTS indexes(
                    version_id TEXT NOT NULL REFERENCES versions(id), fingerprint TEXT NOT NULL,
                    collection TEXT NOT NULL, chunk_count INTEGER NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(version_id,fingerprint));
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('library_id', uuid.uuid4().hex))
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('schema', '1'))
            if db.execute("SELECT value FROM settings WHERE key='schema'").fetchone()[0] != '1':
                raise LibraryError('schema_mismatch')

    @contextmanager
    def connection(self):
        if not self.root.is_dir():
            raise LibraryError('library_not_initialized')
        db = sqlite3.connect(str(self.database), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, action, detail):
        db.execute('INSERT INTO events(at,action,detail) VALUES(?,?,?)', (now(), action, canonical(detail)))

    def import_pdf(self, path, metadata):
        prepared = inspect_pdf(path, metadata)  # extraction failure cannot alter an active version
        self.initialize()
        meta, version = prepared['metadata'], prepared['version_id']
        target = self.root / 'objects' / (meta['sha256'] + '.pdf')
        if target.exists():
            if digest(target.read_bytes()) != meta['sha256']:
                raise LibraryError('stored_object_hash_mismatch')
        else:
            with target.open('xb') as stream:
                stream.write(prepared['data'])
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            other = db.execute('SELECT doc_id FROM versions WHERE file_hash=? AND doc_id<>?',
                               (meta['sha256'], meta['doc_id'])).fetchone()
            if other:
                raise LibraryError('same_file_already_registered_under_another_document')
            if db.execute('SELECT id FROM versions WHERE id=?', (version,)).fetchone():
                return {'version_id': version, 'duplicate': True, 'chunks': len(prepared['chunks'])}
            db.execute('INSERT OR IGNORE INTO documents(id) VALUES(?)', (meta['doc_id'],))
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',
                       (version, meta['doc_id'], meta['sha256'], canonical(meta), canonical(prepared['pages']), now()))
            db.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)',
                [(c['id'], version, c['page'], c['printed_page'], c['start'], c['end'], c['text'], c['text_sha256'])
                 for c in prepared['chunks']])
            self.event(db, 'import_staged', {'doc_id': meta['doc_id'], 'version_id': version})
        return {'version_id': version, 'duplicate': False, 'chunks': len(prepared['chunks'])}

    def library_id(self):
        with self.connection() as db:
            return db.execute("SELECT value FROM settings WHERE key='library_id'").fetchone()[0]

    def versions(self):
        with self.connection() as db:
            rows = db.execute('''SELECT v.id,v.doc_id,v.file_hash,v.metadata,v.created_at,
                d.active_version,COUNT(c.id) AS chunks FROM versions v JOIN documents d ON d.id=v.doc_id
                JOIN chunks c ON c.version_id=v.id GROUP BY v.id ORDER BY v.doc_id,v.created_at,v.id''').fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value['active'] = value.pop('active_version') == value['id']
                value['metadata'] = json.loads(value['metadata'])
                value['vector_indexes'] = [dict(r) for r in db.execute(
                    'SELECT fingerprint,collection,chunk_count FROM indexes WHERE version_id=?', (value['id'],))]
                value['medical_review_status'] = 'not_verified'
                result.append(value)
            return result

    def activate(self, version_id):
        self.verify(version_id)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT doc_id FROM versions WHERE id=?', (version_id,)).fetchone()
            if not row:
                raise LibraryError('unknown_version')
            db.execute('UPDATE documents SET active_version=? WHERE id=?', (version_id, row['doc_id']))
            self.event(db, 'activate_for_local_retrieval', {'doc_id': row['doc_id'], 'version_id': version_id})

    def disable(self, doc_id):
        with self.connection() as db:
            if db.execute('UPDATE documents SET active_version=NULL WHERE id=?', (doc_id,)).rowcount != 1:
                raise LibraryError('unknown_document')
            self.event(db, 'disable', {'doc_id': doc_id})

    def active_ids(self):
        with self.connection() as db:
            return [r[0] for r in db.execute('SELECT active_version FROM documents WHERE active_version IS NOT NULL ORDER BY id')]

    def chunks(self, version_id):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM chunks WHERE version_id=? ORDER BY page,start', (version_id,)).fetchall()
            if not rows:
                raise LibraryError('unknown_or_empty_version')
            return [dict(r) for r in rows]

    def verify(self, version_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()
            if not row:
                raise LibraryError('unknown_version')
            obj = self.root / 'objects' / (row['file_hash'] + '.pdf')
            if not obj.is_file() or digest(obj.read_bytes()) != row['file_hash']:
                raise LibraryError('stored_object_hash_mismatch')
            # Re-hash the original and compare current DB data every time. PDF parsing
            # is reused only inside this library instance for the same immutable metadata.
            cached = self._verified_sources.get(version_id)
            if cached is None or cached[0] != row['metadata']:
                prepared = inspect_pdf(obj, json.loads(row['metadata']))
                prepared.pop('data',None)
                self._verified_sources[version_id] = (row['metadata'],prepared)
            else:
                prepared = cached[1]
            if prepared['version_id'] != version_id or canonical(prepared['pages']) != row['pages']:
                raise LibraryError('stored_source_contract_mismatch')
            actual = [{k: c[k] for k in ('id','page','printed_page','start','end','text','text_sha256')}
                      for c in self.chunks(version_id)]
            if actual != prepared['chunks']:
                raise LibraryError('stored_chunks_mismatch')
            return True

    def mark_indexed(self, version_id, fingerprint, collection):
        self.verify(version_id)
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO indexes VALUES(?,?,?,?,?)',
                       (version_id, fingerprint, collection, len(self.chunks(version_id)), now()))
            self.event(db, 'vector_index_verified', {'version_id': version_id, 'fingerprint': fingerprint,
                                                     'collection': collection})

    def indexed(self, version_id, fingerprint, collection):
        with self.connection() as db:
            row = db.execute('SELECT chunk_count FROM indexes WHERE version_id=? AND fingerprint=? AND collection=?',
                             (version_id, fingerprint, collection)).fetchone()
            return row is not None and row[0] == len(self.chunks(version_id))

    def evidence(self, chunk_id, allowed_versions):
        with self.connection() as db:
            row = db.execute('''SELECT c.*,v.doc_id,v.file_hash,v.metadata FROM chunks c
                JOIN versions v ON v.id=c.version_id WHERE c.id=?''', (chunk_id,)).fetchone()
            if not row or row['version_id'] not in allowed_versions:
                raise LibraryError('evidence_outside_active_snapshot')
            meta = json.loads(row['metadata'])
            return {'id': row['id'], 'version_id': row['version_id'], 'doc_id': row['doc_id'],
                    'title': meta['title'], 'edition': meta['edition'], 'publisher': meta['publisher'],
                    'source_url': meta['source_url'], 'download_url': meta['download_url'],
                    'file_sha256': row['file_hash'], 'page': row['page'], 'printed_page': row['printed_page'],
                    'start': row['start'], 'end': row['end'], 'text': row['text'],
                    'text_sha256': row['text_sha256'], 'scope_note': meta['scope_note'],
                    'medical_review_status': 'not_verified'}


def terms(text):
    text = unicodedata.normalize('NFKC', text).lower()
    # PDF soft line wraps/spaces must not split Chinese terms. Punctuation still separates runs.
    text = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text)
    values = []
    for run in re.findall(r'[\u3400-\u9fff]+|[a-z0-9]+', text):
        if '\u3400' <= run[0] <= '\u9fff':
            values.extend(run[i:i+2] for i in range(len(run)-1))
        else:
            values.append(run)
    return values


def lexical_search(library, query, limit=5, version_ids=None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise LibraryError('invalid_query')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise LibraryError('invalid_limit')
    ids = library.active_ids() if version_ids is None else version_ids
    rows = []
    for version in ids:
        library.verify(version)
        rows.extend(library.chunks(version))
    if not rows:
        return []
    query_terms = set(terms(query))
    counters = [Counter(terms(row['text'])) for row in rows]
    lengths = [sum(c.values()) for c in counters]
    average = max(1, sum(lengths) / len(rows))
    frequencies = Counter(t for c in counters for t in c)
    scored = []
    for row, counter, length in zip(rows, counters, lengths):
        score = 0.0
        for term in query_terms & counter.keys():
            idf = math.log(1 + (len(rows) - frequencies[term] + .5) / (frequencies[term] + .5))
            frequency = counter[term]
            score += idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
        if score > 0:
            scored.append((score, row['id']))
    return [dict(library.evidence(key, ids), retrieval={'channel':'lexical_bm25', 'score':score})
            for score,key in sorted(scored,key=lambda pair:(-pair[0],pair[1]))[:limit]]


def embedding_text(evidence):
    return f"{evidence['title']}\n版本：{evidence['edition']}\nPDF页：{evidence['page']}\n{evidence['text']}"
