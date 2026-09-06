import unittest
from unittest.mock import patch
from qa_bot.jobfinder_source import Document,selected_invitation


class Response:
    def __init__(self,url,text):self.url,self.text=url,text
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def read(self):return self.text.encode()


class JobFinderSourceTests(unittest.TestCase):
    def test_exact_profile_only_and_ambiguous_invitation_rejected(self):
        pages=['<p>Logged in</p>',
            '<div class="cg-card" data-mailbox="other"><span class="cg-name">Other</span></div>'
            '<div class="cg-card" data-mailbox="selected"><span class="cg-name">Test Person</span><br></div>',
            '<div class="cg-msg" data-id="chosen">TP Assessment - Test Login Details</div>',
            '<a href="https://amcatglobal.aspiringminds.com/?fixture=1">To start your test click here</a>']
        calls=[]
        class Client:
            def open(self,url,data=None,timeout=None):
                calls.append(url)
                return Response(url,pages[len(calls)-1])
        with patch('qa_bot.jobfinder_source.build_opener',return_value=Client()):
            self.assertEqual(selected_invitation('Test Person','fixture','fixture'),
                'https://amcatglobal.aspiringminds.com/?fixture=1')
        self.assertTrue(calls[2].endswith('mailbox=selected'))
        self.assertTrue(calls[3].endswith('id=chosen'))
        pages[3]+=pages[3];calls.clear()
        with patch('qa_bot.jobfinder_source.build_opener',return_value=Client()):
            with self.assertRaisesRegex(ValueError,'one authorized SHL invitation'):
                selected_invitation('Test Person','fixture','fixture')

    def test_parser_preserves_names_after_void_elements(self):
        root=Document('<div><input><span class="cg-name">Test &amp; Person</span></div>').root
        names=[n.text for n in root.walk() if n.has_class('cg-name')]
        self.assertEqual(names,['Test & Person'])
