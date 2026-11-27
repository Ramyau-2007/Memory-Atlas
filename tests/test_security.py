import asyncio
import os
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from starlette.requests import Request

from app.security import RequestSecurityMiddleware, limit_request, request_limiter


class RequestSecurityMiddlewareTests(unittest.TestCase):
    def setUp(self):
        request_limiter.clear()

    @staticmethod
    def _request(client_ip, cloudflare_ip=None):
        headers = []
        if cloudflare_ip is not None:
            headers.append((b"cf-connecting-ip", cloudflare_ip.encode("ascii")))
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/api/register",
            "headers": headers,
            "client": (client_ip, 12345),
        })

    def test_render_rate_limits_use_validated_cloudflare_client_ip(self):
        with patch.dict(os.environ, {
            "RENDER": "true",
            "REGISTER_RATE_LIMIT": "1",
            "REGISTER_RATE_WINDOW_SECONDS": "60",
        }, clear=False):
            check = limit_request("register-test", "REGISTER_RATE_LIMIT", "REGISTER_RATE_WINDOW_SECONDS", 5, 3600)
            check(self._request("10.0.0.1", "198.51.100.1"))
            check(self._request("10.0.0.1", "198.51.100.2"))
            with self.assertRaises(HTTPException) as error:
                check(self._request("10.0.0.1", "198.51.100.1"))
        self.assertEqual(error.exception.status_code, 429)

    def test_non_render_requests_do_not_trust_forwarded_ip_headers(self):
        with patch.dict(os.environ, {
            "RENDER": "false",
            "REGISTER_RATE_LIMIT": "1",
            "REGISTER_RATE_WINDOW_SECONDS": "60",
        }, clear=False):
            check = limit_request("register-local-test", "REGISTER_RATE_LIMIT", "REGISTER_RATE_WINDOW_SECONDS", 5, 3600)
            check(self._request("127.0.0.1", "198.51.100.1"))
            with self.assertRaises(HTTPException) as error:
                check(self._request("127.0.0.1", "198.51.100.2"))
        self.assertEqual(error.exception.status_code, 429)

    def test_invalid_render_client_ip_falls_back_to_proxy_peer(self):
        with patch.dict(os.environ, {
            "RENDER": "true",
            "REGISTER_RATE_LIMIT": "1",
            "REGISTER_RATE_WINDOW_SECONDS": "60",
        }, clear=False):
            check = limit_request("register-invalid-ip-test", "REGISTER_RATE_LIMIT", "REGISTER_RATE_WINDOW_SECONDS", 5, 3600)
            check(self._request("10.0.0.1", "not-an-ip"))
            with self.assertRaises(HTTPException) as error:
                check(self._request("10.0.0.1", "also-not-an-ip"))
        self.assertEqual(error.exception.status_code, 429)

    def test_chunked_body_is_stopped_after_crossing_configured_limit(self):
        seen=[]
        downstream_called=False

        async def application(scope, receive, send):
            nonlocal downstream_called
            downstream_called=True
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})

        scope={
            'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
            'scheme':'http','path':'/api/register','raw_path':b'/api/register',
            'query_string':b'','root_path':'','headers':[(b'host',b'testserver')],
            'client':('127.0.0.1',1234),'server':('testserver',80),
        }
        messages=[
            {'type':'http.request','body':b'12345678','more_body':True},
            {'type':'http.request','body':b'abcdefgh','more_body':False},
        ]
        async def receive(): return messages.pop(0)
        async def send(message): seen.append(message)

        with patch.dict(os.environ,{'MAX_REQUEST_BODY_BYTES':'10'}):
            asyncio.run(RequestSecurityMiddleware(application)(scope,receive,send))

        self.assertFalse(downstream_called)
        start=next(message for message in seen if message['type']=='http.response.start')
        self.assertEqual(start['status'],413)
        body=b''.join(message.get('body',b'') for message in seen if message['type']=='http.response.body')
        self.assertIn(b'too large',body)


if __name__=='__main__':
    unittest.main()
