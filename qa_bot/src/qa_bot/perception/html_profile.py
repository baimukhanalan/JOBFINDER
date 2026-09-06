"""Parse the explicit qa-v1 DOM contract; no scripts are executed here."""
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)

    def text(self) -> str:
        if "hidden" in self.attrs or self.attrs.get("aria-hidden") == "true":
            return ""
        if self.tag in ("script", "style", "template"):
            return ""
        return " ".join(" ".join(c.text() if isinstance(c, Node) else c
                               for c in self.children).split())

    def find(self, predicate) -> list:
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if "hidden" in child.attrs or child.attrs.get("aria-hidden") == "true":
                    continue
                if child.tag in ("script", "style", "template"):
                    continue
                if predicate(child):
                    found.append(child)
                found.extend(child.find(predicate))
        return found


class ProfileParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict((k, v or "") for k, v in attrs))
        self.stack[-1].children.append(node)
        if tag not in ("input", "img", "source", "br", "hr", "meta", "link", "wbr", "area", "embed"):
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)
