"""Lossless character spans; deliberately no rewriting or medical inference."""
import hashlib

FIELDS = {'desc': '疾病描述', 'cause': '病因', 'prevent': '预防',
          'symptom': '症状', 'check': '检查', 'cure_way': '治疗方式原文'}
MAX_CHARS = 600


def spans(text, size=MAX_CHARS):
    if not isinstance(text, str) or not isinstance(size, int) or size < 1:
        raise ValueError('Text and positive span size required')
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind(mark, start + size // 2, end) for mark in '。；！？\n')
            if boundary >= 0:
                end = boundary + 1
        yield start, end, text[start:end]
        start = end


def chunks_for_record(record, proof):
    disease = record['name'].strip()
    for field, label in FIELDS.items():
        raw = record.get(field)
        values = [(None, raw)] if isinstance(raw, str) else list(enumerate(raw)) if isinstance(raw, list) else []
        for index, value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            for start, end, text in spans(value):
                source = {key: proof[key] for key in ('file', 'file_sha256', 'line', 'record_id')}
                source.update(disease=disease, field=field, start=start, end=end)
                if index is not None:
                    source['index'] = index
                identity = f"{source['file_sha256']}:{source['line']}:{field}:{index}:{start}:{end}"
                yield {'id': 'TextChunk:' + hashlib.sha256(identity.encode()).hexdigest(),
                       'disease': disease, 'field': field, 'label': label, 'text': text,
                       'source': source, 'medical_review_status': 'not_verified'}
