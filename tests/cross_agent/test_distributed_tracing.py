import json
import os
import pytest
import webtest

from newrelic.api.transaction import current_transaction
from newrelic.api.wsgi_application import wsgi_application
from newrelic.common.encoding_utils import DistributedTracePayload
from newrelic.common.object_wrapper import function_wrapper

from testing_support.fixtures import (override_application_settings,
        validate_transaction_metrics, validate_transaction_event_attributes,
        validate_error_event_attributes, validate_attributes)

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
JSON_DIR = os.path.normpath(os.path.join(CURRENT_DIR, 'fixtures',
    'distributed_tracing'))

_parameters_list = ['account_id', 'comment', 'expected_metrics',
        'force_sampled_true', 'inbound_payloads', 'intrinsics',
        'major_version', 'minor_version', 'outbound_payloads',
        'raises_exception', 'span_events_enabled', 'test_name',
        'transport_type', 'trusted_account_key', 'web_transaction']
_parameters = ','.join(_parameters_list)


def load_tests():
    result = []
    path = os.path.join(JSON_DIR, 'distributed_tracing.json')
    with open(path, 'r') as fh:
        tests = json.load(fh)

    for test in tests:
        values = (test.get(param, None) for param in _parameters_list)
        param = pytest.param(*values, id=test.get('test_name'))
        result.append(param)

    return result


def override_distributed_trace_payload_version(major_version, minor_version):
    @function_wrapper
    def _override(wrapped, instance, args, kwargs):
        original_version = DistributedTracePayload.version
        DistributedTracePayload.version = (major_version, minor_version)
        try:
            return wrapped(*args, **kwargs)
        finally:
            DistributedTracePayload.version = original_version

    return _override


def assert_payload(payload, payload_assertions):
    assert payload

    # flatten payload so it matches the test:
    #   payload['d']['ac'] -> payload['d.ac']
    d = payload.pop('d')
    for key, value in d.items():
        payload['d.%s' % key] = value

    for expected in payload_assertions.get('expected', []):
        assert expected in payload

    for unexpected in payload_assertions.get('unexpected', []):
        assert unexpected not in payload

    for key, value in payload_assertions.get('exact', {}).items():
        assert key in payload
        if isinstance(value, list):
            value = tuple(value)
        assert payload[key] == value


@wsgi_application()
def target_wsgi_application(environ, start_response):
    status = '200 OK'
    output = b'hello world'
    response_headers = [('Content-type', 'text/html; charset=utf-8'),
                        ('Content-Length', str(len(output)))]

    txn = current_transaction()
    txn.set_transaction_name(test_settings['test_name'])

    if not test_settings['web_transaction']:
        txn.background_task = True

    if test_settings['raises_exception']:
        try:
            1 / 0
        except ZeroDivisionError:
            txn.record_exception()

    extra_inbound_payloads = test_settings['extra_inbound_payloads']
    for payload, expected_result in extra_inbound_payloads:
        result = txn.accept_distributed_trace_payload(payload,
                test_settings['transport_type'])
        assert result is expected_result

    outbound_payloads = test_settings['outbound_payloads']
    if outbound_payloads:
        for payload_assertions in outbound_payloads:
            payload = txn.create_distributed_trace_payload()
            assert_payload(payload, payload_assertions)

    start_response(status, response_headers)
    return [output]


test_application = webtest.TestApp(target_wsgi_application)


@pytest.mark.parametrize(_parameters, load_tests())
def test_distributed_tracing(account_id, comment, expected_metrics,
        force_sampled_true, inbound_payloads, intrinsics, major_version,
        minor_version, outbound_payloads, raises_exception,
        span_events_enabled, test_name, transport_type, trusted_account_key,
        web_transaction):

    extra_inbound_payloads = []
    if transport_type != 'HTTP':
        # Since wsgi_application calls accept_distributed_trace_payload
        # automatically with transport_type='HTTP', we must defer this call
        # until we can specify the transport type.
        extra_inbound_payloads.append((inbound_payloads.pop(), True))
    elif not inbound_payloads:
        # In order to assert that accept_distributed_trace_payload returns
        # False in this instance, we defer.
        extra_inbound_payloads.append((inbound_payloads, False))
    elif len(inbound_payloads) > 1:
        extra_inbound_payloads.append((inbound_payloads[1], False))

    global test_settings
    test_settings = {
        'test_name': test_name,
        'web_transaction': web_transaction,
        'raises_exception': raises_exception,
        'extra_inbound_payloads': extra_inbound_payloads,
        'outbound_payloads': outbound_payloads,
        'transport_type': transport_type,
    }

    override_settings = {
        'distributed_tracing.enabled': True,
        'span_events.enabled': span_events_enabled,
        'account_id': account_id,
        'trusted_account_key': trusted_account_key
    }

    common_required = intrinsics['common']['expected']
    common_forgone = intrinsics['common']['unexpected']
    common_exact = intrinsics['common'].get('exact', {})

    txn_intrinsics = intrinsics.get('Transaction', {})
    txn_event_required = {'agent': [], 'user': [],
            'intrinsic': txn_intrinsics.get('expected', [])}
    txn_event_required['intrinsic'].extend(common_required)
    txn_event_forgone = {'agent': [], 'user': [],
            'intrinsic': txn_intrinsics.get('unexpected', [])}
    txn_event_forgone['intrinsic'].extend(common_forgone)
    txn_event_exact = {'agent': {}, 'user': {},
            'intrinsic': txn_intrinsics.get('exact', {})}
    txn_event_exact['intrinsic'].update(common_exact)

    headers = {}
    if inbound_payloads:
        payload = json.dumps(inbound_payloads[0])
        headers['newrelic'] = payload

    @validate_transaction_metrics(test_name,
            rollup_metrics=expected_metrics,
            background_task=not web_transaction)
    @validate_transaction_event_attributes(
            txn_event_required, txn_event_forgone, txn_event_exact)
    @validate_attributes('intrinsic', common_required, common_forgone)
    def _test():
        response = test_application.get('/', headers=headers)
        assert 'X-NewRelic-App-Data' not in response.headers

    if raises_exception:
        error_event_required = {'agent': [], 'user': [],
                'intrinsic': common_required}
        error_event_forgone = {'agent': [], 'user': [],
                'intrinsic': common_forgone}
        error_event_exact = {'agent': {}, 'user': {},
                'intrinsic': common_exact}
        _test = validate_error_event_attributes(error_event_required,
                error_event_forgone, error_event_exact)(_test)

    _test = override_application_settings(override_settings)(_test)
    _test = override_distributed_trace_payload_version(
            major_version, minor_version)(_test)

    _test()
