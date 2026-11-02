"""Every German text of the pages has an English entry in static/i18n.js. `mise run test`.

Mirrors how the pages look texts up: `t("…")` calls, and `translateStatic()` for the fixed HTML (text nodes,
`title`, `placeholder`, `aria-label` outside `data-dyn` containers). The review page only translates inside
`header`, `dialog` and `#drop` and takes whole `data-t-html` elements (white space collapsed) as one text.
"""
import functools
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
PAGES = {"index.html": ("header", "dialog", "#drop"), "live.html": ("body",)}  # where translateStatic() looks
# texts that are the same in both languages or are not words: no entry needed
SAME_IN_ENGLISH = {"/Users/…/MacWhisper-Exporte", "Esc", "Deutsch", "English", "DE", "EN", "OK", "KI", "Code", "Replay", "Policy", "Command", "Review", "Live", "Text", "Typ", "Lane", "Element", "Hotspot", "Name"}

CALL = re.compile(r"""\bt\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`([^`$\\]*)`)""")
KEY = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', re.M)


@functools.cache
def english_keys() -> set[str]:
    src = (STATIC / "i18n.js").read_text()
    en = src[src.index("const EN"):src.index("const DE")]
    return {json.loads(f'"{k}"') for k in KEY.findall(en)}


class FixedTexts(HTMLParser):
    """Texts of the page that translateStatic() looks up: outside script, style, svg, head and data-dyn."""

    def __init__(self, roots: tuple[str, ...]):
        super().__init__()
        self.roots = roots
        self.found: set[str] = set()
        self.stack: list[tuple[str, bool, bool]] = []  # (tag, skips its content, is a translated root)
        self.in_body = False

    VOID = {"input", "br", "img", "meta", "link", "hr", "use", "path", "circle", "rect"}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "body":
            self.in_body = True
        root = tag in self.roots or (f"#{a.get('id')}" in self.roots)
        skip = tag in {"script", "style", "svg", "template", "textarea"} or "data-dyn" in a or "data-t-html" in a
        if self.live() and not skip:
            for name in ("title", "placeholder", "aria-label"):
                if a.get(name):
                    self.found.add(a[name].strip())
        if tag not in self.VOID:
            self.stack.append((tag, skip, root))

    def live(self) -> bool:
        """Inside a translated root and not inside anything that is skipped."""
        return self.in_body and not any(s for _, s, _ in self.stack) and any(r for _, _, r in self.stack)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        text = data.strip()
        if text and self.live():
            self.found.add(text)


def words(text: str) -> bool:
    return bool(re.search(r"[A-Za-zÄÖÜäöüß]{2}", text))


@pytest.mark.parametrize("page", PAGES)
def test_t_calls_have_english_entries(page):
    src = (STATIC / page).read_text()
    called = {json.loads('"' + (m[1] or m[2] or m[3]).replace('"', '\\"') + '"') if m[1] is not None else (m[2] or m[3]) for m in CALL.finditer(src)}
    missing = sorted(k for k in called if words(k) and k not in english_keys() and k not in SAME_IN_ENGLISH)
    assert not missing, f"{page}: no English text for t(): {missing}"


@pytest.mark.parametrize("page", PAGES)
def test_fixed_texts_have_english_entries(page):
    src = (STATIC / page).read_text()
    parser = FixedTexts(PAGES[page])
    parser.feed(src)
    whole = {re.sub(r"\s+", " ", m[1]).strip() for m in re.finditer(r"<(\w+)[^>]*\bdata-t-html\b[^>]*>(.*?)</\1>", src, re.S) for m in [(m[1], m[2])]}
    missing = sorted(k for k in parser.found | whole if words(k) and k not in english_keys() and k not in SAME_IN_ENGLISH)
    assert not missing, f"{page}: no English text for the fixed text: {missing}"
