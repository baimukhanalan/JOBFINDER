"""Read exactly one named, preselected JobFinder invitation using its HTTP API."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener


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
    if not login or not password:raise ValueError('JobFinder credentials required in environment')
    base='https://jobs.systeam.kz'
    client=build_opener(HTTPCookieProcessor(CookieJar()))
    def read(path,data=None):
        with client.open(base+path,data=data,timeout=20) as response:
            if urlsplit(response.url).netloc!='jobs.systeam.kz':raise ValueError('unexpected JobFinder redirect')
            return Document(response.read().decode()).root
    read('/login',urlencode({'login':login,'password':password}).encode())
    root=read('/mail/candidates?'+urlencode({'tab':'all','stage':'action_needed','q':profile}))
    cards=[n for n in root.walk() if n.has_class('cg-card') and
        any(k.has_class('cg-name') and k.text.strip()==profile for k in n.walk())]
    if len(cards)!=1:raise ValueError('one exact preselected profile required')
    mailbox=cards[0].attrs.get('data-mailbox')
    if not mailbox:raise ValueError('selected profile mailbox missing')
    thread=read('/mail/candidates/thread?'+urlencode({'mailbox':mailbox}))
    messages=[n for n in thread.walk() if n.has_class('cg-msg') and 'TP Assessment - Test Login Details' in n.text]
    if len(messages)!=1:raise ValueError('one TP invitation required')
    message_id=messages[0].attrs.get('data-id')
    if not message_id:raise ValueError('selected invitation id missing')
    message=read('/mail/candidates/message?'+urlencode({'id':message_id}))
    links=[n.attrs.get('href') for n in message.walk() if n.tag=='a' and n.text.strip()=='To start your test click here']
    if len(links)!=1 or not links[0].startswith('https://amcatglobal.aspiringminds.com/'):
        raise ValueError('one authorized SHL invitation required')
    return links[0]
