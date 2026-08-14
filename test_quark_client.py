"""夸克上传客户端：drive-pc 主机、parallel_upload、X-Oss-Hash-Ctx。"""
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from luboman.core import quark_client
from luboman.core.quark_client import (
    QuarkApiError,
    QuarkClient,
    _Sha1Running,
    encode_hash_ctx,
)


class Sha1RunningTest(unittest.TestCase):
    def test_matches_hashlib_for_common_inputs(self):
        samples = [b'', b'abc', b'a' * 63, b'a' * 64, b'a' * 65, b'x' * 4096]
        for data in samples:
            running = _Sha1Running()
            running.update(data)
            self.assertEqual(
                running.hexdigest(),
                hashlib.sha1(data).hexdigest(),
                msg=f'len={len(data)}',
            )

    def test_snapshot_empty_is_initial_state(self):
        ctx = _Sha1Running().snapshot()
        self.assertEqual(ctx['hash_type'], 'sha1')
        self.assertEqual(ctx['h0'], str(0x67452301))
        self.assertEqual(ctx['Nl'], '0')
        self.assertEqual(ctx['Nh'], '0')
        self.assertEqual(ctx['data'], '')
        self.assertEqual(ctx['num'], '0')

    def test_encode_hash_ctx_is_ascii_base64_json(self):
        ctx = _Sha1Running().snapshot()
        encoded = encode_hash_ctx(ctx)
        payload = json.loads(__import__('base64').b64decode(encoded))
        self.assertEqual(payload['hash_type'], 'sha1')
        self.assertEqual(payload['h0'], ctx['h0'])


class QuarkClientUploadContractTest(unittest.TestCase):
    def test_api_host_is_drive_pc(self):
        self.assertEqual(quark_client.API, 'https://drive-pc.quark.cn/1/clouddrive')

    def test_oss_url_strips_https_scheme(self):
        url = QuarkClient._oss_url({
            'bucket': 'bucket',
            'upload_url': 'https://oss-cn-zhangjiakou.aliyuncs.com',
            'obj_key': 'obj/key',
        })
        self.assertEqual(url, 'https://bucket.oss-cn-zhangjiakou.aliyuncs.com/obj/key')

    def test_upload_pre_sends_parallel_upload(self):
        client = QuarkClient('__pus=x; __puus=y')
        client._request = MagicMock(return_value={'data': {'task_id': 't1'}})
        client.upload_pre('a.mp4', 'video/mp4', 10, '0')
        _args, kwargs = client._request.call_args
        self.assertEqual(_args[0], '/file/upload/pre')
        body = kwargs['json_body']
        self.assertTrue(body['parallel_upload'])
        self.assertTrue(body['ccp_hash_update'])
        self.assertEqual(body['pdir_fid'], '0')

    def test_upload_part_includes_hash_ctx_from_second_part(self):
        client = QuarkClient('__pus=x; __puus=y')
        client._upload_auth = MagicMock(return_value='OSS auth')
        pre = {
            'data': {
                'auth_info': 'info',
                'task_id': 'task',
                'bucket': 'bkt',
                'obj_key': 'key',
                'upload_id': 'uid',
                'upload_url': 'http://oss.example.com',
            }
        }
        ctx = _Sha1Running().snapshot()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {'ETag': '"etag-2"'}
        fake_resp.text = ''

        with patch('luboman.core.quark_client.requests.put', return_value=fake_resp) as put:
            etag = client.upload_part(pre, 'video/mp4', 2, b'data', hash_ctx=ctx)

        self.assertEqual(etag, '"etag-2"')
        headers = put.call_args.kwargs['headers']
        self.assertIn('X-Oss-Hash-Ctx', headers)
        auth_meta = client._upload_auth.call_args[0][1]
        self.assertIn('X-Oss-Hash-Ctx:', auth_meta)
        self.assertIn('x-oss-user-agent:aliyun-sdk-js/1.0.0', auth_meta)

    def test_upload_part_first_part_has_no_hash_ctx(self):
        client = QuarkClient('__pus=x; __puus=y')
        client._upload_auth = MagicMock(return_value='OSS auth')
        pre = {
            'data': {
                'auth_info': 'info',
                'task_id': 'task',
                'bucket': 'bkt',
                'obj_key': 'key',
                'upload_id': 'uid',
                'upload_url': 'http://oss.example.com',
            }
        }
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {'ETag': '"etag-1"'}
        fake_resp.text = ''

        with patch('luboman.core.quark_client.requests.put', return_value=fake_resp) as put:
            client.upload_part(pre, 'video/mp4', 1, b'data')

        self.assertNotIn('X-Oss-Hash-Ctx', put.call_args.kwargs['headers'])
        auth_meta = client._upload_auth.call_args[0][1]
        self.assertNotIn('X-Oss-Hash-Ctx', auth_meta)

    def test_upload_file_raises_when_part_size_missing(self):
        client = QuarkClient('__pus=x; __puus=y')
        client.upload_pre = MagicMock(return_value={'data': {'task_id': 't'}})
        client.upload_hash = MagicMock(return_value=False)

        with self.assertRaises(QuarkApiError):
            import tempfile
            import os
            fd, path = tempfile.mkstemp(suffix='.bin')
            try:
                os.write(fd, b'hello')
                os.close(fd)
                client.upload_file(path, '0')
            finally:
                os.unlink(path)


if __name__ == '__main__':
    unittest.main()
