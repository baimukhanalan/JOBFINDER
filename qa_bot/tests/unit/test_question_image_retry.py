import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from playwright.async_api import Error
from qa_bot.solvers.analytical import fetch_question_image

class QuestionImageRetryTests(unittest.IsolatedAsyncioTestCase):
    def session(self, effects, numbers=('3','3','3')):
        response=SimpleNamespace(status=200,headers={'content-type':'image/png'},body=AsyncMock(return_value=b'png'))
        request=SimpleNamespace(get=AsyncMock(side_effect=[response if x=='ok' else x for x in effects]))
        page=SimpleNamespace(request=request,evaluate=AsyncMock(side_effect=[{'number':x} for x in numbers]))
        logs=[]
        return SimpleNamespace(page=page,log=lambda name,row:logs.append((name,row))),logs

    async def test_socket_failure_retries_same_question(self):
        s,logs=self.session([Error('socket hang up'),'ok'])
        self.assertEqual(await fetch_question_image(s,'https://example.test/figure','3'),(b'png','image/png'))
        self.assertEqual(s.page.request.get.await_count,2)
        self.assertEqual(len(logs),1)

    async def test_question_change_prevents_second_request(self):
        s,_=self.session([Error('socket hang up'),'ok'],('3','4'))
        with self.assertRaisesRegex(ValueError,'question changed'):
            await fetch_question_image(s,'https://example.test/figure','3')
        self.assertEqual(s.page.request.get.await_count,1)

    async def test_permanent_failure_is_bounded(self):
        s,_=self.session([Error('socket hang up')]*3)
        with self.assertRaises(Error):await fetch_question_image(s,'https://example.test/figure','3')
        self.assertEqual(s.page.request.get.await_count,3)
