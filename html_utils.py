"""HTML validation, sanitization, and text extraction helpers."""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Doctype

_FENCE_START_RE = re.compile(r"^\s*```(?:html)?\s*", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"\s*```\s*$")
_IMPORT_RE = re.compile(
    r"@import\s+(?:url\([^)]*\)|[\"'][^\"']*[\"'])\s*;?",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(
    r'url\(\s*(?P<quote>["\']?)(?P<url>[^)]*?)(?P=quote)\s*\)',
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r'https?://[^\s<>"\'()\x60]+', re.IGNORECASE)
_REMOVED_TAGS = {
    "audio",
    "base",
    "embed",
    "frame",
    "frameset",
    "iframe",
    "link",
    "math",
    "object",
    "picture",
    "portal",
    "source",
    "svg",
    "track",
    "video",
}
_DANGEROUS_URL_ATTRS = {
    "action",
    "background",
    "data",
    "formaction",
    "longdesc",
    "poster",
    "profile",
    "xlink:href",
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


def _trim_extracted_url(value: str) -> str:
    """Remove punctuation that commonly follows a URL in prose."""
    value = value.rstrip(".,;:!?\u3002\uff0c\uff1b\uff1a\uff01\uff1f")
    while value.endswith(")") and value.count("(") < value.count(")"):
        value = value[:-1]
    while value.endswith("]") and value.count("[") < value.count("]"):
        value = value[:-1]
    while value.endswith("}") and value.count("{") < value.count("}"):
        value = value[:-1]
    return value


def normalize_image_url(value: object) -> str | None:
    """Normalize one image URL and reject non-network or malformed sources.

    Args:
        value: Candidate URL.

    Returns:
        A normalized HTTP or HTTPS URL, or None when the source is unsafe.
    """
    text = html_lib.unescape(str(value or "")).strip().strip("\"'")
    text = _trim_extracted_url(text)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not hostname or parsed.username is not None or parsed.password is not None:
        return None
    normalized_host = hostname.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def extract_image_urls(value: object) -> list[str]:
    """Extract normalized HTTP and HTTPS URLs from a prompt or text value.

    Args:
        value: Prompt text that may contain image URLs.

    Returns:
        Deduplicated normalized URLs in their first-seen order.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _HTTP_URL_RE.finditer(str(value or "")):
        normalized = normalize_image_url(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def normalize_image_urls(values: object) -> list[str]:
    """Normalize and deduplicate an iterable of authorized image URLs.

    Args:
        values: URL string or iterable of URL-like values.

    Returns:
        Deduplicated normalized HTTP and HTTPS URLs.
    """
    if isinstance(values, str):
        candidates: Iterable[object] = extract_image_urls(values)
    elif isinstance(values, Iterable):
        candidates = values
    else:
        candidates = ()
    normalized_urls: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        normalized = normalize_image_url(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            normalized_urls.append(normalized)
    return normalized_urls


def is_allowed_image_url(value: object, allowed_image_urls: object = ()) -> bool:
    """Return whether one URL exactly matches the normalized allowlist.

    Args:
        value: Candidate image URL.
        allowed_image_urls: Authorized image URL values.

    Returns:
        True only when the candidate is a normalized HTTP or HTTPS allowlist item.
    """
    normalized = normalize_image_url(value)
    return bool(normalized and normalized in normalize_image_urls(allowed_image_urls))


def _image_csp_sources(allowed_image_urls: object = ()) -> list[str]:
    """Convert authorized URLs into CSP-compatible image sources."""
    sources: list[str] = []
    seen: set[str] = set()
    for value in normalize_image_urls(allowed_image_urls):
        parsed = urlsplit(value)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for source in (origin, value.rsplit("#", 1)[0]):
            if source not in seen:
                seen.add(source)
                sources.append(source)
    return sources


def build_csp(allowed_image_urls: object = ()) -> str:
    """Build the restrictive CSP used by generated theater documents.

    Args:
        allowed_image_urls: URLs authorized by the current theater prompt.

    Returns:
        A CSP that permits only inline theater code and authorized image origins.
    """
    image_sources = " ".join(_image_csp_sources(allowed_image_urls)) or "'none'"
    return (
        "default-src 'none'; "
        f"img-src {image_sources}; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "connect-src 'none'; "
        "font-src 'none'; "
        "media-src 'none'; "
        "frame-src 'none'; "
        "child-src 'none'; "
        "worker-src 'none'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "navigate-to 'none'; "
        "sandbox allow-scripts"
    )


def _sanitize_srcset(value: object, allowed: set[str]) -> str:
    """Keep only authorized URLs and their srcset descriptors."""
    candidates: list[str] = []
    for candidate in str(value or "").split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        normalized = normalize_image_url(parts[0])
        if normalized in allowed:
            candidates.append(" ".join([normalized, *parts[1:]]))
    return ", ".join(candidates)


def _sanitize_css(value: object, allowed: set[str]) -> str:
    """Remove CSS imports and keep only authorized url() references."""
    value = _IMPORT_RE.sub("", str(value or ""))

    def replace_url(match: re.Match[str]) -> str:
        normalized = normalize_image_url(match.group("url"))
        if normalized not in allowed:
            return "none"
        return f'url("{normalized}")'

    return _CSS_URL_RE.sub(replace_url, value)


def sanitize_html(value: str, allowed_image_urls: object = ()) -> str:
    """Validate and return model HTML without rewriting its contents.

    Args:
        value: Complete HTML returned by the theater API.
        allowed_image_urls: Kept for compatibility with older callers. It is
            intentionally ignored because model HTML is preserved verbatim.

    Returns:
        The complete HTML document with its original contents.

    Raises:
        ValueError: If the input is empty or not a complete HTML document.
    """
    value = strip_markdown_fence(value)
    if not is_complete_html(value):
        raise ValueError("API 返回的 HTML 不完整。")

    return value

    allowed = set(normalize_image_urls(allowed_image_urls))
    soup = BeautifulSoup(value, "html.parser")
    for tag in list(soup.find_all(_REMOVED_TAGS)):
        tag.decompose()
    for tag in list(soup.find_all("script")):
        if any(str(name).lower() == "src" for name in tag.attrs):
            tag.decompose()

    for tag in soup.find_all(True):
        for attr_name in list(tag.attrs):
            lowered = str(attr_name).lower()
            attr_value = tag.attrs.get(attr_name)
            if lowered.startswith("on"):
                del tag.attrs[attr_name]
                continue
            if lowered == "src":
                if tag.name == "img":
                    normalized = normalize_image_url(attr_value)
                    if normalized in allowed:
                        tag.attrs[attr_name] = normalized
                    else:
                        del tag.attrs[attr_name]
                else:
                    del tag.attrs[attr_name]
                continue
            if lowered == "srcset":
                if tag.name == "img":
                    sanitized = _sanitize_srcset(attr_value, allowed)
                    if sanitized:
                        tag.attrs[attr_name] = sanitized
                    else:
                        del tag.attrs[attr_name]
                else:
                    del tag.attrs[attr_name]
                continue
            if lowered in _DANGEROUS_URL_ATTRS:
                del tag.attrs[attr_name]
                continue
            if lowered == "href":
                href = str(attr_value or "").strip()
                if not href.startswith("#"):
                    del tag.attrs[attr_name]
                continue
            if lowered == "style":
                tag.attrs[attr_name] = _sanitize_css(attr_value, allowed)

    for style in soup.find_all("style"):
        style.string = _sanitize_css(style.get_text(), allowed)

    for meta in list(soup.find_all("meta")):
        http_equiv = str(meta.get("http-equiv", "")).lower()
        if http_equiv in {"refresh", "content-security-policy"}:
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
    if not soup.head.find(
        "meta", attrs={"name": re.compile(r"^viewport$", re.IGNORECASE)}
    ):
        viewport = soup.new_tag("meta")
        viewport["name"] = "viewport"
        viewport["content"] = "width=device-width, initial-scale=1"
        soup.head.insert(1, viewport)
    csp = soup.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = build_csp(allowed)
    soup.head.insert(2, csp)

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
