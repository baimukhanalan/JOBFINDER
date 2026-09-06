import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from playwright.async_api import async_playwright
from qa_bot.live_session import Session
from qa_bot.solvers.analytical import options,click_node,label_info,selected_input_evidence


async def backend(session,selector):
    document=await session.cdp.send('DOM.getDocument')
    selected=await session.cdp.send('DOM.querySelector',{'nodeId':document['root']['nodeId'],'selector':selector})
    return (await session.cdp.send('DOM.describeNode',{'nodeId':selected['nodeId']}))['node']['backendNodeId']


class AnalyticalEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_selection_records_actual_changed_input_value(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<div><input id="a" type="radio" name="choices" value="1"><label for="a">Alpha</label></div>
                <div><input id="b" type="radio" name="choices" value="2" onchange="this.value='42'"><label for="b">Beta</label></div>''')
                with tempfile.TemporaryDirectory() as d:
                    session=Session(page,Path(d));session.cdp=await page.context.new_cdp_session(page)
                    nodes=[await backend(session,'label[for=a]'),await backend(session,'label[for=b]')]
                    infos=[await label_info(session,n) for n in nodes]
                    self.assertEqual([o['input_value'] for o in infos],['1','2'])
                    await click_node(session,nodes[1])
                    info=await label_info(session,nodes[1]);self.assertTrue(info['checked'])
                    result=selected_input_evidence(info,question=SimpleNamespace(question_id='analytical-7',content_hash='a'*64),option=SimpleNamespace(id='option-2'),index=1)
                    self.assertEqual(result['selected_input_value'],'42')
                    self.assertEqual(result['selected_option_position'],2)
                    self.assertEqual(result['question_id'],'analytical-7')
                    self.assertEqual(result['selected_input_value_json_sha256'],hashlib.sha256(json.dumps('42').encode()).hexdigest())
                    self.assertEqual(result['selected_input_value_numeric_json_sha256'],hashlib.sha256(b'42').hexdigest())
            finally:await browser.close()

    async def test_long_or_sensitive_value_is_only_hashed(self):
        secret='sk_sensitive_value_not_for_logs'
        result=selected_input_evidence({'input_count':1,'input_value':secret,'input_value_attribute':secret},
            question=SimpleNamespace(question_id='analytical-1',content_hash='b'*64),option=SimpleNamespace(id='option-1'),index=0)
        self.assertNotIn(secret,json.dumps(result))
        self.assertNotIn('selected_input_value',result)
        self.assertEqual(result['selected_input_value_sha256'],hashlib.sha256(secret.encode()).hexdigest())

    async def test_ambiguous_input_parent_is_tagged_without_a_value(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('<div><input id="a" type="radio" value="1"><input type="radio" value="2"><label for="a">Alpha</label></div>')
                with tempfile.TemporaryDirectory() as d:
                    session=Session(page,Path(d));session.cdp=await page.context.new_cdp_session(page)
                    info=await label_info(session,await backend(session,'label[for=a]'))
                    self.assertEqual(info['input_count'],2);self.assertIsNone(info['input_value'])
            finally:await browser.close()
