"""Blank-body / bogus-attachment fix for HTML-only ATS mail (e.g. a Greenhouse
'Security code' email = multipart/related with a text/html body + an inline cid logo).
No network. See mailcrm._is_attachment / _html_to_text / _parse_full.
"""
from email.message import EmailMessage

from backend.tools import mailcrm


def _security_code_email() -> EmailMessage:
    """A multipart/related HTML-only email with a cid-inline logo, like the real one."""
    m = EmailMessage()
    m["Subject"] = "Security code for your application to Nebius"
    m["From"] = "Greenhouse <no-reply@us.greenhouse-mail.io>"
    m["To"] = "inkar.kozhabekova5001@takhet.com"
    m.set_content("placeholder")  # replaced below
    m.clear_content()
    m.add_alternative(
        '<html><body><p>Hi Inkar,</p><p>Copy and paste this code into the security '
        'code field:</p><p>VnUQ6qIR</p><img src="cid:logo@greenhouse"></body></html>',
        subtype="html")
    # attach the inline logo as a cid part (this is what used to be mis-flagged)
    m.get_payload()[0].add_related(b"\x89PNG\r\n\x1a\n" + b"0" * 200,
                                   maintype="image", subtype="png", cid="<logo@greenhouse>")
    return m


def test_cid_inline_logo_is_not_an_attachment():
    m = _security_code_email()
    atts = mailcrm._attachments(m)
    assert atts == [], f"cid-inline logo must not be an attachment, got {atts}"
    assert not any(mailcrm._is_attachment(p) for p in m.walk()), "has_att must be False"


def test_real_attachment_is_still_detected():
    m = EmailMessage()
    m["Subject"] = "with a real file"
    m.set_content("see attached")
    m.add_attachment(b"%PDF-1.4 resume", maintype="application", subtype="pdf",
                     filename="resume.pdf")
    atts = mailcrm._attachments(m)
    assert len(atts) == 1 and atts[0]["filename"] == "resume.pdf"


def test_html_to_text_extracts_readable_body_with_code():
    html = ('<html><head><style>p{color:red}</style></head><body><p>Hi Inkar,</p>'
            '<p>Your code:</p><p>VnUQ6qIR</p></body></html>')
    txt = mailcrm._html_to_text(html)
    assert "VnUQ6qIR" in txt, "the security code must survive the html->text strip"
    assert "<" not in txt and "color:red" not in txt, "tags + style must be stripped"
    assert "Hi Inkar" in txt
