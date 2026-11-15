"""Minimal, safe HTML metadata extraction (no JS, no external fetches)."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PageMetadata:
    title: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_url: str | None = None
    description: str | None = None
    canonical: str | None = None
    robots: str | None = None
    classes: set[str] = field(default_factory=set)
    text_fragments: list[str] = field(default_factory=list)

    def public_text(self) -> list[str]:
        values = [self.title, self.og_title, self.og_description, self.description, *self.text_fragments]
        return [v for v in values if v]


class _MetaParser(HTMLParser):
    _TEXT_CLASSES = ("tgme_page_title", "tgme_page_description", "tgme_page_extra")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta = PageMetadata()
        self._in_title = False
        self._capture_depth = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        for cls in attr.get("class", "").split():
            self.meta.classes.add(cls)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").lower()
            content = attr.get("content", "").strip()[:2000]
            if key == "og:title":
                self.meta.og_title = content
            elif key == "og:description":
                self.meta.og_description = content
            elif key == "og:url":
                self.meta.og_url = content
            elif key == "description":
                self.meta.description = content
            elif key == "robots":
                self.meta.robots = content.lower()
        elif tag == "link" and "canonical" in attr.get("rel", "").lower().split():
            self.meta.canonical = attr.get("href", "").strip()[:2048] or None
        if self._capture_depth:
            self._capture_depth += 1
        elif tag == "div" and any(c in attr.get("class", "").split() for c in self._TEXT_CLASSES):
            self._capture_depth = 1
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._capture_depth:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                text = " ".join(" ".join(self._buffer).split())
                if text:
                    self.meta.text_fragments.append(text[:2000])

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.meta.title = ((self.meta.title or "") + data).strip()[:500]
        if self._capture_depth:
            self._buffer.append(data)


def parse_metadata(document: str) -> PageMetadata:
    parser = _MetaParser()
    try:
        parser.feed(document[:500_000])
        parser.close()
    except (AssertionError, ValueError):
        # Malformed markup: keep whatever was parsed before the error.
        logger.debug("HTML metadata parse stopped early on malformed markup")
    meta = parser.meta
    for attr in ("title", "og_title", "og_description", "description"):
        value = getattr(meta, attr)
        if value:
            setattr(meta, attr, html.unescape(value))
    return meta
