import unittest
from unittest.mock import patch
from qa_bot.jobfinder_source import Document,selected_invitation,InvitationSelectionError


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
            with self.assertRaisesRegex(InvitationSelectionError,'ambiguous_shl_invitation_link'):
                selected_invitation('Test Person','fixture','fixture')

    def select_with_messages(self,messages):
        pages=['<p>Logged in</p>',
               '<div class="cg-card" data-mailbox="selected"><span class="cg-name">Test Person</span></div>',messages]
        calls=[]
        class Client:
            def open(self,url,data=None,timeout=None):
                calls.append(url)
                return Response(url,pages[len(calls)-1])
        return Client(),calls

    def test_other_providers_are_no_tp_invitation_without_fallback(self):
        client,calls=self.select_with_messages('<div class="cg-msg" data-id="a">Conduent assessment</div>'
                                              '<div class="cg-msg" data-id="b">TTEC test login</div>')
        with patch('qa_bot.jobfinder_source.build_opener',return_value=client):
            with self.assertRaises(InvitationSelectionError) as error:
                selected_invitation('Test Person','fixture-login','fixture-password')
        self.assertEqual(error.exception.reason,'no_tp_invitation')
        self.assertEqual(error.exception.as_dict(),{'phase':'invitation_selection','reason':'no_tp_invitation'})
        self.assertEqual(len(calls),3)
        self.assertNotIn('/message?',calls[-1])

    def test_multiple_tp_messages_are_ambiguous_without_selecting_one(self):
        client,calls=self.select_with_messages('<div class="cg-msg" data-id="a">TP Assessment - Test Login Details</div>'
                                              '<div class="cg-msg" data-id="b">TP Assessment - Test Login Details</div>')
        with patch('qa_bot.jobfinder_source.build_opener',return_value=client):
            with self.assertRaises(InvitationSelectionError) as error:
                selected_invitation('Test Person','fixture-login','fixture-password')
        self.assertEqual(error.exception.reason,'ambiguous_tp_invitation')
        self.assertEqual(len(calls),3)

    def test_transport_failures_do_not_embed_urls_or_credentials(self):
        with patch('qa_bot.jobfinder_source.build_opener') as builder:
            builder.return_value.open.side_effect=OSError('https://host.invalid/?token=secret fixture-password')
            with self.assertRaises(InvitationSelectionError) as error:
                selected_invitation('Test Person','fixture-login','fixture-password')
        self.assertEqual(str(error.exception),'jobfinder_request_failed')
        self.assertNotIn('secret',str(error.exception.as_dict()))

    def test_machine_error_reason_is_allowlisted(self):
        with self.assertRaisesRegex(ValueError,'unknown invitation error reason'):
            InvitationSelectionError('https://host.invalid/?token=secret')

    def test_parser_preserves_names_after_void_elements(self):
        root=Document('<div><input><span class="cg-name">Test &amp; Person</span></div>').root
        names=[n.text for n in root.walk() if n.has_class('cg-name')]
        self.assertEqual(names,['Test & Person'])
