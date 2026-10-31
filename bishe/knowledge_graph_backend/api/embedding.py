"""DashScope native embedding adapter. No calls or credential loading at import.

Independent of Django/Neo4j/chat. Real transport requires explicit opt-in.
"""
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MODEL = 'qwen3.7-text-embedding'
DIMENSIONS = 1024
INSTRUCTION = ('Given a Chinese medical knowledge question, retrieve relevant passages '
               'from medical guidelines and health education documents.')
CONFIG_PATH = Path(__file__).resolve().parents[1] / '.env.embedding'
API_PATH = '/api/v1/services/embeddings/text-embedding/text-embedding'


class EmbeddingError(Exception):
    """Only static diagnostic codes, never raw HTTP bodies or credentials."""


@dataclass(frozen=True)
class EmbeddingConfig:
    host: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.host, str) or not re.fullmatch(
                r'ws-[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', self.host):
            raise EmbeddingError('invalid_beijing_api_host')
        if (not isinstance(self.api_key, str) or not self.api_key.startswith('sk-')
                or not self.api_key.isascii() or any(c.isspace() for c in self.api_key)):
            raise EmbeddingError('missing_or_invalid_api_key')

    @classmethod
    def load(cls, path=CONFIG_PATH):
        from dotenv import dotenv_values
        # This dedicated file never changes or loads the existing chat .env.
        values = dotenv_values(path, interpolate=False) if Path(path).is_file() else {}
        return cls(host=os.environ.get('DASHSCOPE_API_HOST', values.get('DASHSCOPE_API_HOST', '')),
                   api_key=os.environ.get('DASHSCOPE_API_KEY', values.get('DASHSCOPE_API_KEY', '')))

    def public_contract(self):
        return {'provider': 'aliyun-dashscope-native', 'region': 'cn-beijing',
                'host': self.host, 'model': MODEL, 'dimensions': DIMENSIONS,
                'normalization': 'l2-v1', 'distance': 'Cosine',
                'query_instruction': INSTRUCTION, 'input_contract': 'medical-text-v1',
                'weight_revision': 'provider_alias_not_pinned'}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.public_contract(), sort_keys=True).encode()).hexdigest()


def build_payload(texts, role):
    if role not in ('query', 'document'):
        raise EmbeddingError('invalid_text_type')
    if (not isinstance(texts, list) or not 1 <= len(texts) <= 20
            or any(not isinstance(text, str) or not text.strip() for text in texts)):
        raise EmbeddingError('invalid_text_batch')
    if any(len(text.encode('utf-8')) > 8192 for text in texts):
        raise EmbeddingError('text_byte_limit')
    if sum(len(text.encode('utf-8')) for text in texts) > 65536:
        raise EmbeddingError('batch_byte_limit')
    parameters = {'dimension': DIMENSIONS, 'output_type': 'dense', 'text_type': role}
    if role == 'query':
        parameters['instruct'] = INSTRUCTION
    return {'model': MODEL, 'input': {'texts': texts}, 'parameters': parameters}


def usage_tokens(body):
    usage = body.get('usage') if isinstance(body, dict) else None
    value = usage.get('total_tokens') if isinstance(usage, dict) else None
    return value if type(value) is int and value > 0 else None


def validate_response(body, count):
    if not isinstance(body, dict) or body.get('status_code', 200) != 200 or body.get('code'):
        raise EmbeddingError('provider_rejected')
    if 'model' in body and body['model'] != MODEL:
        raise EmbeddingError('response_model_mismatch')
    output = body.get('output')
    items = output.get('embeddings') if isinstance(output, dict) else None
    if not isinstance(items, list) or len(items) != count:
        raise EmbeddingError('response_item_count')
    vectors = [None] * count
    for item in items:
        if not isinstance(item, dict):
            raise EmbeddingError('response_item_schema')
        index, vector = item.get('text_index'), item.get('embedding')
        if type(index) is not int or not 0 <= index < count or vectors[index] is not None:
            raise EmbeddingError('response_text_index')
        if not isinstance(vector, list) or len(vector) != DIMENSIONS:
            raise EmbeddingError('response_dimensions')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise EmbeddingError('response_nonfinite_vector')
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm <= 1e-12:
            raise EmbeddingError('response_zero_or_invalid_norm')
        vectors[index] = [value / norm for value in vector]
    return vectors


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EmbeddingError('duplicate_response_key')
        result[key] = value
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EmbeddingError('redirect_refused')


class NativeHTTPTransport:
    def __init__(self, *, allow_external=False):
        if not allow_external:
            raise EmbeddingError('external_transport_requires_opt_in')
        self.opener = urllib.request.build_opener(_NoRedirect())

    def post(self, config, payload, timeout):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        request = urllib.request.Request('https://' + config.host + API_PATH, data=data,
            headers={'Authorization': 'Bearer ' + config.api_key, 'Content-Type': 'application/json'},
            method='POST')
        try:
            # urllib has no automatic application retry here; redirects are refused.
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise EmbeddingError('response_byte_limit')
            return json.loads(raw, object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _: None)
        except EmbeddingError:
            raise
        except urllib.error.HTTPError as error:
            code = 'http_status_' + str(error.code)
            # Keep only known categorical diagnostics; provider messages may echo inputs.
            try:
                detail = json.loads(error.read(65536)) if error.fp is not None else {}
                allowed = {'AccessDenied', 'Forbidden', 'InvalidApiKey', 'InvalidParameter',
                           'ModelNotFound', 'Throttling', 'Arrearage', 'Unauthorized'}
                if isinstance(detail, dict) and isinstance(detail.get('code'), str) and detail['code'] in allowed:
                    code += ':' + detail['code']
            except Exception:
                pass
            raise EmbeddingError(code) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise EmbeddingError('transport_unavailable_or_timeout') from None
        except (ValueError, TypeError, RecursionError):
            raise EmbeddingError('invalid_response_json') from None


class EmbeddingClient:
    def __init__(self, config, *, transport=None, allow_external=False):
        self.config = config
        self.transport = transport if transport is not None else NativeHTTPTransport(allow_external=allow_external)
        self.records = []

    def embed(self, texts, role, *, timeout=20):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise EmbeddingError('invalid_timeout')
        payload = build_payload(texts, role)
        record = {'role': role, 'items': len(texts), 'status': 'started',
                  'total_tokens': None, 'config_fingerprint': self.config.fingerprint()}
        self.records.append(record)
        started = time.monotonic()
        try:
            body = self.transport.post(self.config, payload, timeout)
            record['total_tokens'] = usage_tokens(body)
            vectors = validate_response(body, len(texts))
            record['status'] = 'ok'
            return vectors
        except EmbeddingError as error:
            record['status'] = 'failed'
            record['error'] = str(error)
            raise
        except Exception:
            record.update(status='failed', error='unexpected_transport_error')
            raise EmbeddingError('unexpected_transport_error') from None
        finally:
            record['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
