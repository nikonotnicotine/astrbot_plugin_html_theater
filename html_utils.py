"""HTML validation, sanitization, and text extraction helpers."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Doctype

_FENCE_START_RE = re.compile(r"^\s*```(?:html)?\s*", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"\s*```\s*$")
_IMPORT_RE = re.compile(
    r"@import\s+(?:url\([^)]*\)|[\"'][^\"']*[\"'])\s*;?",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(r"url\(\s*[^)]*\)", re.IGNORECASE)
_DANGEROUS_TAGS = {
    "script",
    "img",
    "iframe",
    "object",
    "embed",
    "link",
    "base",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "option",
    "video",
    "audio",
    "source",
    "picture",
    "svg",
    "math",
    "canvas",
}


def strip_markdown_fence(value: str) -> str:
    """Remove one surrounding Markdown HTML fence from an API response.

    Args:
        value: Raw model response text.

    Returns:
        Response text without a surrounding code fence.
    """
    value = _FENCE_START_RE.sub("", str(value or ""), count=1)
    return _FENCE_END_RE.sub("", value, count=1).strip()


def is_complete_html(value: str) -> bool:
    """Return whether a response contains an explicit complete HTML document.

    Args:
        value: Candidate HTML response.

    Returns:
        True when both an opening html tag and a closing html tag are present.
    """
    value = strip_markdown_fence(value)
    return bool(
        value
        and re.search(r"<html(?:\s|>)", value, re.IGNORECASE)
        and re.search(r"</html\s*>", value, re.IGNORECASE)
    )


def _sanitize_css(value: str) -> str:
    """Remove CSS constructs that can fetch external resources.

    Args:
        value: CSS text from a style tag or style attribute.

    Returns:
        CSS text with imports and url() references removed.
    """
    value = _IMPORT_RE.sub("", str(value or ""))
    return _CSS_URL_RE.sub("none", value)


def sanitize_html(value: str) -> str:
    """Sanitize model HTML while retaining document structure and inline CSS.

    Args:
        value: Complete HTML returned by the theater API.

    Returns:
        A standalone sanitized HTML document.

    Raises:
        ValueError: If the input is empty or not a complete HTML document.
    """
    value = strip_markdown_fence(value)
    if not is_complete_html(value):
        raise ValueError("API 返回的 HTML 不完整。")

    soup = BeautifulSoup(value, "html.parser")
    for tag in list(soup.find_all(_DANGEROUS_TAGS)):
        tag.decompose()

    for tag in soup.find_all(True):
        for attr_name in list(tag.attrs):
            lowered = attr_name.lower()
            attr_value = tag.attrs.get(attr_name)
            if lowered.startswith("on"):
                del tag.attrs[attr_name]
                continue
            if lowered in {"src", "srcset", "action", "formaction", "poster"}:
                del tag.attrs[attr_name]
                continue
            if lowered == "href":
                href = str(attr_value or "").strip()
                if not href.startswith("#"):
                    del tag.attrs[attr_name]
                continue
            if lowered == "style":
                tag.attrs[attr_name] = _sanitize_css(str(attr_value or ""))

    for style in soup.find_all("style"):
        style.string = _sanitize_css(style.get_text())

    for meta in list(soup.find_all("meta")):
        if str(meta.get("http-equiv", "")).lower() == "refresh":
            meta.decompose()

    if soup.html is None:
        raise ValueError("API 返回内容缺少 html 根元素。")
    if soup.head is None:
        soup.html.insert(0, soup.new_tag("head"))
    if soup.body is None:
        soup.html.append(soup.new_tag("body"))
    if not soup.head.find("meta", attrs={"charset": True}):
        charset = soup.new_tag("meta")
        charset["charset"] = "utf-8"
        soup.head.insert(0, charset)

    for item in list(soup.contents):
        if isinstance(item, Doctype):
            item.extract()
    return "<!doctype html>\n" + str(soup.html)


def extract_html_text(value: str) -> str:
    """Extract readable theater text while excluding CSS and metadata.

    Args:
        value: Sanitized or raw HTML.

    Returns:
        Collapsed plain text suitable for search and one-shot LLM injection.
    """
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for tag in soup.find_all(
        {"style", "script", "head", "noscript", "template", "svg", "canvas"}
    ):
        tag.decompose()
    lines = []
    for line in soup.get_text("\n").splitlines():
        text = re.sub(r"\s+", " ", line).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)
