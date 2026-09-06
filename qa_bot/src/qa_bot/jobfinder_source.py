"""Read exactly one named, preselected JobFinder invitation using its HTTP API."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener

INVITATION_ERROR_REASONS = frozenset({
    'missing_jobfinder_credentials', 'jobfinder_request_failed', 'unexpected_jobfinder_redirect',
    'profile_not_found', 'ambiguous_profile', 'profile_mailbox_missing',
    'no_tp_invitation', 'ambiguous_tp_invitation', 'invitation_id_missing',
    'no_shl_invitation_link', 'ambiguous_shl_invitation_link', 'invalid_shl_invitation_origin',
})


class InvitationSelectionError(ValueError):
    """A safe machine-readable failure, without URLs, message bodies or secrets."""
    def __init__(self, reason):
        if reason not in INVITATION_ERROR_REASONS:
            raise ValueError('unknown invitation error reason')
        self.reason = reason
        super().__init__(reason)

    def as_dict(self):
        return {'phase': 'invitation_selection', 'reason': self.reason}


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    parts: list = field(default_factory=list)

    @property
    def text(self):
        return ''.join(p.text if isinstance(p,Node) else p for p in self.parts)

    def walk(self):
        yield self
        for part in self.parts:
            if isinstance(part,Node):yield from part.walk()

    def has_class(self,name):return name in self.attrs.get('class','').split()


class Document(HTMLParser):
    def __init__(self,html):
        super().__init__(convert_charrefs=True)
        self.root=Node('root');self.stack=[self.root];self.feed(html)

    def handle_starttag(self,tag,attrs):
        node=Node(tag,dict(attrs));self.stack[-1].parts.append(node)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append(node)

    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                del self.stack[i:];break

    def handle_data(self,data):self.stack[-1].parts.append(data)


def selected_invitation(profile,login,password):
    if not login or not password:raise InvitationSelectionError('missing_jobfinder_credentials')
    base='https://jobs.systeam.kz'
    client=build_opener(HTTPCookieProcessor(CookieJar()))
    def read(path,data=None):
        try:
            with client.open(base+path,data=data,timeout=20) as response:
                endpoint=urlsplit(response.url)
                if endpoint.scheme!='https' or endpoint.netloc!='jobs.systeam.kz':
                    raise InvitationSelectionError('unexpected_jobfinder_redirect')
                return Document(response.read().decode()).root
        except InvitationSelectionError:
            raise
        except Exception:
            raise InvitationSelectionError('jobfinder_request_failed') from None
    read('/login',urlencode({'login':login,'password':password}).encode())
    root=read('/mail/candidates?'+urlencode({'tab':'all','stage':'action_needed','q':profile}))
    cards=[n for n in root.walk() if n.has_class('cg-card') and
        any(k.has_class('cg-name') and k.text.strip()==profile for k in n.walk())]
    if not cards:raise InvitationSelectionError('profile_not_found')
    if len(cards)>1:raise InvitationSelectionError('ambiguous_profile')
    mailbox=cards[0].attrs.get('data-mailbox')
    if not mailbox:raise InvitationSelectionError('profile_mailbox_missing')
    thread=read('/mail/candidates/thread?'+urlencode({'mailbox':mailbox}))
    messages=[n for n in thread.walk() if n.has_class('cg-msg') and 'TP Assessment - Test Login Details' in n.text]
    if not messages:raise InvitationSelectionError('no_tp_invitation')
    if len(messages)>1:raise InvitationSelectionError('ambiguous_tp_invitation')
    message_id=messages[0].attrs.get('data-id')
    if not message_id:raise InvitationSelectionError('invitation_id_missing')
    message=read('/mail/candidates/message?'+urlencode({'id':message_id}))
    links=[n.attrs.get('href') for n in message.walk() if n.tag=='a' and n.text.strip()=='To start your test click here']
    if not links:raise InvitationSelectionError('no_shl_invitation_link')
    if len(links)>1:raise InvitationSelectionError('ambiguous_shl_invitation_link')
    try:target=urlsplit(links[0] or '')
    except ValueError:raise InvitationSelectionError('invalid_shl_invitation_origin') from None
    if target.scheme!='https' or target.netloc!='amcatglobal.aspiringminds.com' or not target.path.startswith('/'):
        raise InvitationSelectionError('invalid_shl_invitation_origin')
    return links[0]
