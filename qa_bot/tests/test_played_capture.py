import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from qa_bot.audio.played_capture import capture_html

class PlayedCaptureTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        source='/SpeechAssessmentBank/stimulus/fixture.mp3'
        response=SimpleNamespace(status=200,headers={'content-type':'audio/mpeg'},body=AsyncMock(return_value=b'fixture-audio'))
        receipt=SimpleNamespace(status=201,json=AsyncMock(return_value={'file':'audio/fixture.mp3','sha256':'a'*64}))
        request=SimpleNamespace(get=AsyncMock(side_effect=[RuntimeError('transient'),response]),post=AsyncMock(return_value=receipt))
        page=SimpleNamespace(request=request,evaluate=AsyncMock(return_value=[{'player':'html','path':source,'id':'23','order':1}]))
        return SimpleNamespace(page=page,preflight=False,profile_id='fixture',test_id='test'),{'url':'https://qbdata-amcat.s3.amazonaws.com'+source+'?signature=fixture','id':'23','order':1}

    async def test_transport_retry_and_created_receipt_preserve_source_without_signature(self):
        session,item=self.session()
        with patch.dict('os.environ',{'QA_LOCAL_BRIDGE_TOKEN':'t'*32}):result=await capture_html(session,item)
        self.assertEqual(result['file'],'audio/fixture.mp3')
        self.assertEqual(session.page.request.get.await_count,2)
        headers=session.page.request.post.call_args.kwargs['headers']
        self.assertNotIn('?',headers['X-Prompt-Source'])

    async def test_unplayed_or_wrong_origin_never_fetches(self):
        for mode in ('unplayed','origin','preflight'):
            session,item=self.session()
            if mode=='unplayed':item['order']=2
            elif mode=='origin':item['url']='https://unrelated.example/audio.mp3'
            else:session.preflight=True
            with self.assertRaises(ValueError):await capture_html(session,item)
            session.page.request.get.assert_not_called()
