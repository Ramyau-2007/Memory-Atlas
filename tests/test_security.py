import asyncio
import os
import unittest
from unittest.mock import patch

from app.security import RequestSecurityMiddleware


class RequestSecurityMiddlewareTests(unittest.TestCase):
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
