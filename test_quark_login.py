"""夸克扫码登录：token / 轮询必须携带 client_id + request_id，否则 App 会报二维码过期。"""
import unittest
from unittest.mock import MagicMock, patch

from luboman.core import quark_login


class _FakeResponse:
    def __init__(self, payload, status_code=200, cookies=None):
        self._payload = payload
        self.status_code = status_code
        self.cookies = cookies or {}

    def json(self):
        return self._payload


class QuarkLoginCasParamsTest(unittest.TestCase):
    def test_cas_params_bind_client_and_request_id(self):
        params = quark_login.QuarkLoginSession._cas_params('rid-1', token='tok-1')
        self.assertEqual(params['client_id'], '532')
        self.assertEqual(params['v'], '1.2')
        self.assertEqual(params['request_id'], 'rid-1')
        self.assertEqual(params['token'], 'tok-1')

    def test_cas_params_omit_token_when_fetching(self):
        params = quark_login.QuarkLoginSession._cas_params('rid-2')
        self.assertNotIn('token', params)
        self.assertEqual(params['request_id'], 'rid-2')


class QuarkLoginHttpTest(unittest.TestCase):
    def test_fetch_token_sends_client_id_and_request_id(self):
        session = quark_login.QuarkLoginSession()
        http = MagicMock()
        http.get.return_value = _FakeResponse({
            'status': 2000000,
            'data': {'members': {'token': 'scan-token'}},
        })

        with patch.object(quark_login.uuid, 'uuid4') as fake_uuid:
            fake_uuid.return_value.hex = 'fixed-request-id'
            token, request_id = session._fetch_token(http)

        self.assertEqual(token, 'scan-token')
        self.assertEqual(request_id, 'fixed-request-id')
        _args, kwargs = http.get.call_args
        self.assertEqual(_args[0], quark_login._TOKEN_URL)
        self.assertEqual(kwargs['params'], {
            'client_id': '532',
            'v': '1.2',
            'request_id': 'fixed-request-id',
        })

    def test_poll_reuses_same_request_id(self):
        session = quark_login.QuarkLoginSession()
        http = MagicMock()
        http.get.return_value = _FakeResponse({
            'status': 2000000,
            'data': {'members': {'service_ticket': 'st-1'}},
        })

        ticket = session._poll(http, 'scan-token', 'fixed-request-id')

        self.assertEqual(ticket, 'st-1')
        _args, kwargs = http.get.call_args
        self.assertEqual(_args[0], quark_login._POLL_URL)
        self.assertEqual(kwargs['params'], {
            'client_id': '532',
            'v': '1.2',
            'request_id': 'fixed-request-id',
            'token': 'scan-token',
        })

    def test_exchange_sends_lw_scan(self):
        session = quark_login.QuarkLoginSession()
        http = MagicMock()
        http.get.return_value = _FakeResponse({'success': True})
        http.cookies = {'__puus': 'a', '__pus': 'b'}

        with patch.object(quark_login.requests.utils, 'dict_from_cookiejar',
                          return_value={'__puus': 'a', '__pus': 'b'}):
            cookie = session._exchange(http, 'st-1')

        self.assertIn('__puus=a', cookie)
        _args, kwargs = http.get.call_args
        self.assertEqual(_args[0], quark_login._EXCHANGE_URL)
        self.assertEqual(kwargs['params'], {'st': 'st-1', 'lw': 'scan'})

    def test_qr_url_keeps_official_scan_page(self):
        url = quark_login.QuarkLoginSession._build_qr_url('scan-token')
        self.assertTrue(url.startswith('https://su.quark.cn/4_eMHBJ?'))
        self.assertIn('token=scan-token', url)
        self.assertIn('client_id=532', url)
        self.assertIn('ssb=weblogin', url)


if __name__ == '__main__':
    unittest.main()
