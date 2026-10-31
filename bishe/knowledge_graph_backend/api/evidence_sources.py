"""Public source metadata and backwards-compatible history storage (no migration)."""
import json
import re
from urllib.parse import urlsplit

HEADING = '\n\n资料出处（原始内容尚待医学审核）：'
MARKER = '\n<!-- medical-sources-v1:'


def https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password else ''
    except ValueError:
        return ''


def public_sources(facts):
    rows = []
    for fact in facts:
        eid = fact.get('evidence_id', '')
        if not re.fullmatch(r'E\d{1,4}', eid):
            continue
        for source in fact.get('sources', []):
            common = {'evidence_id': eid, 'medical_review_status': 'not_verified'}
            if source.get('kind') == 'document':
                version = source.get('version_id', '')
                if not re.fullmatch('[0-9a-f]{64}', version) or type(source.get('page')) is not int or source['page'] < 1:
                    continue
                rows.append({**common, 'kind': 'document', 'version_id': version,
                    'title': str(source.get('title', ''))[:240], 'edition': str(source.get('edition', ''))[:120],
                    'publisher': str(source.get('publisher', ''))[:240], 'page': source['page'],
                    'printed_page': str(source.get('printed_page', ''))[:40],
                    'scope_note': str(source.get('scope_note', ''))[:1200],
                    'excerpt': str(fact.get('value', ''))[:1000],
                    'source_url': https_url(source.get('source_url')),
                    'file_url': '/api/documents/' + version + '/file/'})
            elif type(source.get('line')) is int and source['line'] > 0:
                rows.append({**common, 'kind': 'graph', 'title': '医疗知识图谱原始资料',
                    'disease': str(fact.get('disease', ''))[:120], 'label': str(fact.get('label', ''))[:120],
                    'line': source['line']})
            if len(rows) >= 256:
                return rows
    return rows


def source_appendix(facts):
    sources = public_sources(facts)
    if not sources:
        return ''
    lines = []
    for row in sources:
        if row['kind'] == 'document':
            lines.append(f"[{row['evidence_id']}] {row['title']}（{row['edition']}），PDF第{row['page']}页。")
        else:
            lines.append(f"[{row['evidence_id']}] {row['disease']}的{row['label']}：medical.json 第{row['line']}行")
    # ASCII encoding keeps comment delimiters and untrusted source text inert.
    return HEADING + '\n' + '\n'.join(lines) + MARKER + json.dumps(sources, ensure_ascii=True).replace('<', '\\u003c') + '-->'


def stored_sources(content):
    if not isinstance(content, str) or HEADING not in content:
        return []
    if MARKER not in content:
        facts = []
        for line in content.split(HEADING,1)[1][:20000].splitlines():
            match = re.fullmatch(r'\[([E\d, ]+)\] (.+?)的(.+?)：medical\.json 第(\d+)行',line)
            if not match:
                continue
            for eid in match[1].split(','):
                facts.append({'evidence_id':eid.strip(),'disease':match[2],'label':match[3],
                              'sources':[{'line':int(match[4])}]})
        return public_sources(facts)
    payload = content.rsplit(MARKER, 1)[-1]
    if not payload.endswith('-->') or len(payload) > 250000:
        return []
    try:
        rows = json.loads(payload[:-3])
        if not isinstance(rows, list) or len(rows) > 256:
            return []
        # Reconstruct through the same allowlist. Never trust stored arbitrary URLs/keys.
        facts = []
        for row in rows:
            if not isinstance(row, dict):
                return []
            facts.append({'evidence_id': row.get('evidence_id'), 'value': row.get('excerpt', ''),
                          'disease': row.get('disease', ''), 'label': row.get('label', ''), 'sources': [row]})
        return public_sources(facts)
    except (ValueError, TypeError, KeyError):
        return []
