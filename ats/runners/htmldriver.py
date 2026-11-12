"""A small form-level browser used to replay UI steps without a real browser.

It fetches pages, finds elements by UiPath-style selector attributes (id, tag,
name, aaname, type), fills inputs, submits forms and follows links. It runs no
JavaScript - the twin web UI is server-rendered precisely so that this driver,
Selenium, Playwright and a real UiPath robot all see the same thing.

Navigation is confined to the allowed origins of the environment binding.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

VOID = {"input", "br", "hr", "img", "meta", "link"}


class Blocked(Exception):
    """The automation tried to leave the bound test environment."""


class ElementNotFound(Exception):
    pass


class Node:
    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node] = []
        self.text_parts: list[str] = []

    def text(self) -> str:
        return " ".join("".join(self._all_text()).split())

    def _all_text(self):
        for part in self.text_parts:
            yield part if isinstance(part, str) else "".join(part._all_text())

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def ancestor(self, tag: str) -> "Node | None":
        n = self.parent
        while n is not None and n.tag != tag:
            n = n.parent
        return n


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        self.cur.text_parts.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text_parts.append(data)


class HtmlDriver:
    def __init__(self, allowed_prefixes: list[str], timeout: float = 15.0):
        self.allowed = [p.rstrip("/") for p in allowed_prefixes]
        self.http = httpx.Client(follow_redirects=False, timeout=timeout)
        self.url = ""
        self.dom = Node("document", {}, None)
        self.status = 0

    def _check(self, url: str) -> None:
        if not any(url == p or url.startswith(p + "/") or url.startswith(p + "?") for p in self.allowed):
            raise Blocked(f"navigation to {url} is outside the bound test environment")

    def _load(self, method: str, url: str, data: dict | None = None) -> None:
        for _ in range(10):
            self._check(url)
            resp = self.http.request(method, url, data=data)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                url, method, data = urljoin(url, resp.headers["location"]), "GET", None
                continue
            break
        self.url, self.status = url, resp.status_code
        b = _Builder()
        b.feed(resp.text)
        self.dom = b.root

    def goto(self, url: str) -> None:
        self._load("GET", url)

    # -- element lookup ---------------------------------------------------
    def find(self, sel: dict) -> Node | None:
        want = {k: v for k, v in sel.items() if not k.startswith("_") and k in ("id", "tag", "name", "aaname", "type", "class")}
        if not want:
            return None
        for node in self.dom.walk():
            ok = True
            for k, v in want.items():
                actual = node.tag if k == "tag" else node.text() if k == "aaname" else node.attrs.get(k, "")
                if k == "tag":
                    ok = actual.lower() == v.lower()
                else:
                    ok = fnmatchcase(actual, v) if "*" in v or "?" in v else actual == v
                if not ok:
                    break
            if ok:
                return node
        return None

    def _need(self, sel: dict) -> Node:
        node = self.find(sel)
        if node is None:
            raise ElementNotFound(f"no element matches selector {sel} on {self.url}")
        return node

    def exists(self, sel: dict) -> bool:
        return self.find(sel) is not None

    def get_text(self, sel: dict) -> str:
        node = self._need(sel)
        return node.attrs.get("value", "") if node.tag == "input" else node.text()

    def type_into(self, sel: dict, text: str) -> None:
        node = self._need(sel)
        if node.tag not in ("input", "textarea", "select"):
            raise ElementNotFound(f"element {sel} is not an input")
        node.attrs["value"] = text

    def click(self, sel: dict) -> None:
        node = self._need(sel)
        if node.tag == "a" and node.attrs.get("href"):
            self.goto(urljoin(self.url, node.attrs["href"]))
            return
        form = node if node.tag == "form" else node.ancestor("form")
        if form is None:
            return  # a click on a non-interactive element does nothing
        data = {}
        for el in form.walk():
            name = el.attrs.get("name")
            if not name:
                continue
            if el.tag == "input" and el.attrs.get("type") not in ("submit", "button"):
                data[name] = el.attrs.get("value", "")
            elif el.tag == "textarea":
                data[name] = el.attrs.get("value", el.text())
            elif el.tag == "select":
                if "value" in el.attrs:
                    data[name] = el.attrs["value"]
                else:
                    opts = [o for o in el.walk() if o.tag == "option"]
                    chosen = next((o for o in opts if "selected" in o.attrs), opts[0] if opts else None)
                    data[name] = chosen.attrs.get("value", chosen.text()) if chosen else ""
        action = urljoin(self.url, form.attrs.get("action") or self.url)
        if form.attrs.get("method", "get").lower() == "post":
            self._load("POST", action, data)
        else:
            self._check(action)
            self._load("GET", str(httpx.URL(action, params=data)))

    def close(self) -> None:
        self.http.close()
