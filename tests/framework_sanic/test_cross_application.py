import json
import pytest
import re

from newrelic.common.encoding_utils import deobfuscate

from testing_support.fixtures import (override_application_settings,
        make_cross_agent_headers, validate_analytics_catmap_data,
        validate_transaction_metrics)


ENCODING_KEY = '1234567890123456789012345678901234567890'
_cat_response_header_urls_to_test = (
        ('/', '_target_application:index'),
        ('/streaming', '_target_application:streaming'),
        ('/error', '_target_application:error'),
)


def _get_cat_response_header(raw_response):
    match = re.search(r'X-NewRelic-App-Data: (.*)\r',
            raw_response.decode('utf-8'))
    if match:
        return match.group(1).strip()


@pytest.mark.parametrize(
    'inbound_payload,expected_intrinsics,forgone_intrinsics,cat_id', [

    # Valid payload from trusted account
    (['b854df4feb2b1f06', False, '7e249074f277923d', '5d2957be'],
    {'nr.referringTransactionGuid': 'b854df4feb2b1f06',
    'nr.tripId': '7e249074f277923d',
    'nr.referringPathHash': '5d2957be'},
    [],
    '1#1'),

    # Valid payload from an untrusted account
    (['b854df4feb2b1f06', False, '7e249074f277923d', '5d2957be'],
    {},
    ['nr.referringTransactionGuid', 'nr.tripId', 'nr.referringPathHash'],
    '80#1'),
])
@pytest.mark.parametrize('url,metric_name', _cat_response_header_urls_to_test)
def test_cat_response_headers(app, inbound_payload, expected_intrinsics,
        forgone_intrinsics, cat_id, url, metric_name):

    _base_metrics = [
        ('Function/%s' % metric_name, 1),
    ]
    _custom_settings = {
            'cross_process_id': '1#1',
            'encoding_key': ENCODING_KEY,
            'trusted_account_ids': [1],
            'cross_application_tracer.enabled': True,
            'distributed_tracing.enabled': False,
    }

    @validate_transaction_metrics(
        metric_name,
        scoped_metrics=_base_metrics,
        rollup_metrics=_base_metrics,
    )
    @validate_analytics_catmap_data(
            'WebTransaction/Function/%s' % metric_name,
            expected_attributes=expected_intrinsics,
            non_expected_attributes=forgone_intrinsics)
    @override_application_settings(_custom_settings)
    def _test():
        cat_headers = make_cross_agent_headers(inbound_payload, ENCODING_KEY,
                cat_id)
        raw_response = app.fetch('get', url, headers=dict(cat_headers),
                raw=True)

        if expected_intrinsics:
            # test valid CAT response header
            assert b'X-NewRelic-App-Data' in raw_response, raw_response
            cat_response_header = _get_cat_response_header(raw_response)

            app_data = json.loads(deobfuscate(cat_response_header,
                    ENCODING_KEY))
            assert app_data[0] == cat_id
            assert app_data[1] == ('WebTransaction/Function/%s' % metric_name)
        else:
            assert b'X-NewRelic-App-Data' not in raw_response

    _test()
