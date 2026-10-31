"""No network or database required. Native HTTP contract and vector validation."""
import io
import json
import math
import unittest
import urllib.error
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock, patch

from .embedding import (DIMENSIONS, INSTRUCTION, MODEL, EmbeddingClient, EmbeddingConfig,
                        EmbeddingError, NativeHTTPTransport, _NoRedirect, build_payload,
                        usage_tokens, validate_response)

HOST = 'ws-fixture.cn-beijing.maas.aliyuncs.com'


def body(count=2):
    return {'output': {'embeddings': [{'text_index': index, 'embedding': [float(index + 1)] + [0.] * (DIMENSIONS - 1)}
                                    for index in range(count)]}, 'usage': {'total_tokens': 30}}


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.config = EmbeddingConfig(HOST, 'sk-unit-test-only')
        self.transport = Mock()
        self.transport.post.return_value = body()
        self.client = EmbeddingClient(self.config, transport=self.transport)

    def test_document_and_query_have_distinct_contracts(self):
        document = build_payload(['资料'], 'document')
        query = build_payload(['问题'], 'query')
        self.assertEqual(document['model'], MODEL)
        self.assertEqual(document['input'], {'texts': ['资料']})
        self.assertEqual(document['parameters'], {'text_type': 'document', 'dimension': 1024, 'output_type': 'dense'})
        self.assertNotIn('instruct', document['parameters'])
        self.assertEqual(query['parameters']['instruct'], INSTRUCTION)

    def test_index_reordering_and_normalization(self):
        result = body()
        result['output']['embeddings'].reverse()
        self.transport.post.return_value = result
        vectors = self.client.embed(['a', 'b'], 'document', timeout=3)
        self.assertEqual(len(vectors), 2)
        self.assertTrue(all(math.isclose(math.hypot(*v), 1.) for v in vectors))
        self.transport.post.assert_called_once()
        self.assertEqual(self.transport.post.call_args.args[2], 3)

    def test_validation_preserves_input_order(self):
        result = body()
        result['output']['embeddings'][1]['embedding'] = [0., 1.] + [0.] * 1022
        result['output']['embeddings'].reverse()
        vectors = validate_response(result, 2)
        self.assertEqual(vectors[0][:2], [1., 0.])
        self.assertEqual(vectors[1][:2], [0., 1.])

    def test_wrong_dimensions_are_not_padded(self):
        result = body()
        result['output']['embeddings'][0]['embedding'] = [1., 2.]
        with self.assertRaisesRegex(EmbeddingError, 'response_dimensions'):
            validate_response(result, 2)

    def test_nonfinite_boolean_and_zero_vectors_rejected(self):
        for value in (float('nan'), float('inf'), True, '1'):
            with self.subTest(value=value):
                result = body()
                result['output']['embeddings'][0]['embedding'][0] = value
                with self.assertRaises(EmbeddingError): validate_response(result, 2)
        result = body()
        result['output']['embeddings'][0]['embedding'] = [0.] * DIMENSIONS
        with self.assertRaisesRegex(EmbeddingError, 'response_zero'):
            validate_response(result, 2)

    def test_missing_duplicate_invalid_indices_rejected(self):
        for index in (0, -1, 2, True, '1'):
            result = body()
            result['output']['embeddings'][1]['text_index'] = index
            with self.subTest(index=index), self.assertRaises(EmbeddingError): validate_response(result, 2)
        with self.assertRaises(EmbeddingError): validate_response(body(1), 2)

    def test_invalid_inputs_do_not_call_transport(self):
        for texts, role in (([], 'query'), ([''], 'query'), (['a'] * 21, 'document'),
                            (['a', 1], 'document'), (['字' * 3000], 'query'), (['a'], 'bad')):
            with self.subTest(role=role), self.assertRaises(EmbeddingError): self.client.embed(texts, role)
        self.transport.post.assert_not_called()

    def test_timeout_bounds(self):
        for timeout in (0, 31, float('nan'), True):
            with self.assertRaises(EmbeddingError): self.client.embed(['a'], 'query', timeout=timeout)
        self.transport.post.assert_not_called()

    def test_failed_validation_keeps_known_usage(self):
        self.transport.post.return_value = {'usage': {'total_tokens': 35}, 'output': {}}
        with self.assertRaises(EmbeddingError): self.client.embed(['a'], 'query')
        self.assertEqual(self.client.records[0]['total_tokens'], 35)
        self.assertEqual(self.client.records[0]['status'], 'failed')

    def test_unknown_usage_not_zero(self):
        for item in ({}, {'usage': {'total_tokens': True}}, {'usage': {'total_tokens': -1}}, {'usage': {'total_tokens': '30'}}):
            self.assertIsNone(usage_tokens(item))

    def test_no_implicit_external_transport(self):
        with self.assertRaisesRegex(EmbeddingError, 'opt_in'): EmbeddingClient(self.config)

    def test_host_validation_prevents_credential_forwarding(self):
        for host in ('example.com', HOST + '.example.com', 'http://' + HOST, HOST + '/api', 'user@' + HOST):
            with self.assertRaises(EmbeddingError): EmbeddingConfig(host, 'sk-test')

    def test_configuration_repr_and_records_do_not_expose_secrets_or_text(self):
        self.client.embed(['private-text-A', 'private-text-B'], 'document')
        text = repr(self.config) + json.dumps(self.client.records)
        self.assertNotIn('sk-unit-test-only', text)
        self.assertNotIn('private-text', text)

    def test_rotating_key_does_not_change_vector_space(self):
        self.assertEqual(self.config.fingerprint(), EmbeddingConfig(HOST, 'sk-rotated').fingerprint())
        self.assertNotEqual(self.config.fingerprint(), EmbeddingConfig('ws-other.cn-beijing.maas.aliyuncs.com', 'sk-rotated').fingerprint())

    def test_config_file_uses_no_interpolation(self):
        folder = Path(__file__).resolve().parents[3] / 'temp' / ('embedding-test-' + uuid4().hex)
        folder.mkdir()
        path = folder / '.env.embedding'
        path.write_text('DASHSCOPE_API_HOST=' + HOST + '\nDASHSCOPE_API_KEY=sk-${UNSET}\n')
        with patch.dict('os.environ', {}, clear=True):
            config = EmbeddingConfig.load(path)
        self.assertEqual(config.api_key, 'sk-${UNSET}')

    def test_native_http_request_has_expected_endpoint_and_parameters(self):
        transport = NativeHTTPTransport(allow_external=True)
        response = io.BytesIO(json.dumps(body()).encode())
        transport.opener = Mock()
        transport.opener.open.return_value = response
        client = EmbeddingClient(self.config, transport=transport)
        client.embed(['a', 'b'], 'query', timeout=4)
        request = transport.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/api/v1/services/embeddings/text-embedding/text-embedding'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer sk-unit-test-only')
        self.assertEqual(json.loads(request.data)['parameters']['text_type'], 'query')
        self.assertEqual(transport.opener.open.call_args.kwargs['timeout'], 4)

    def test_http_error_is_sanitized_and_not_retried(self):
        transport = NativeHTTPTransport(allow_external=True)
        transport.opener = Mock()
        transport.opener.open.side_effect = urllib.error.HTTPError('https://' + HOST, 401, 'secret-response', {}, None)
        client = EmbeddingClient(self.config, transport=transport)
        with self.assertRaisesRegex(EmbeddingError, '^http_status_401$'): client.embed(['a'], 'query')
        transport.opener.open.assert_called_once()
        self.assertNotIn('secret-response', json.dumps(client.records))

    def test_redirects_refused(self):
        with self.assertRaisesRegex(EmbeddingError, 'redirect_refused'):
            _NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_duplicate_json_keys_refused(self):
        transport = NativeHTTPTransport(allow_external=True)
        transport.opener = Mock()
        transport.opener.open.return_value = io.BytesIO(b'{"output":{},"output":{}}')
        with self.assertRaisesRegex(EmbeddingError, 'duplicate_response_key'):
            transport.post(self.config, build_payload(['a'], 'query'), 2)

    def test_provider_error_retains_only_allowed_code(self):
        transport = NativeHTTPTransport(allow_external=True)
        transport.opener = Mock()
        response = io.BytesIO(json.dumps({'code': 'AccessDenied', 'message': 'secret-input'}).encode())
        transport.opener.open.side_effect = urllib.error.HTTPError('https://' + HOST, 403, '', {}, response)
        client = EmbeddingClient(self.config, transport=transport)
        with self.assertRaisesRegex(EmbeddingError, '^http_status_403:AccessDenied$'):
            client.embed(['a'], 'query')
        self.assertNotIn('secret-input', json.dumps(client.records))

    def test_unknown_provider_code_not_logged(self):
        transport = NativeHTTPTransport(allow_external=True)
        transport.opener = Mock()
        response = io.BytesIO(b'{"code":"secret-input"}')
        transport.opener.open.side_effect = urllib.error.HTTPError('https://' + HOST, 403, '', {}, response)
        with self.assertRaisesRegex(EmbeddingError, '^http_status_403$'):
            transport.post(self.config, build_payload(['a'], 'query'), 2)

    def test_batch_byte_budget(self):
        with self.assertRaisesRegex(EmbeddingError, 'batch_byte_limit'):
            self.client.embed(['x' * 8000] * 9, 'document')
        self.transport.post.assert_not_called()

    def test_unexpected_transport_exception_is_sanitized(self):
        self.transport.post.side_effect = RuntimeError('private-text and credential')
        with self.assertRaisesRegex(EmbeddingError, '^unexpected_transport_error$'):
            self.client.embed(['a'], 'query')
        self.assertNotIn('credential', json.dumps(self.client.records))


if __name__ == '__main__': unittest.main()
