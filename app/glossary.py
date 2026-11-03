"""Small, lecture-scoped exact-term glossary; no shared mutable dictionaries."""
import re
from html import escape
from xml.etree import ElementTree as ET


def parse_glossary(value):
    if not isinstance(value, str) or len(value) > 8000:
        raise ValueError("Glossary must be text, at most 8000 characters")
    pairs = []
    seen = set()
    for line in value.splitlines():
        if not line.strip():
            continue
        source, sep, target = line.partition("=")
        source, target = source.strip(), target.strip()
        if not sep or not source or not target or max(len(source), len(target)) > 120:
            raise ValueError("Use source term = English term, max 120 characters per term")
        if source.casefold() in seen:
            raise ValueError("Duplicate glossary source term")
        seen.add(source.casefold())
        pairs.append((source, target))
    if len(pairs) > 40:
        raise ValueError("At most 40 glossary terms per lecture")
    return tuple(pairs)


def protect(text, pairs):
    if not pairs:
        return text, False
    lookup = {s.casefold(): t for s, t in pairs}
    # Longest match wins. Boundaries protect English/Indonesian substrings;
    # Chinese/Japanese terms can occur without whitespace.
    patterns = []
    for source in sorted((s for s, _ in pairs), key=len, reverse=True):
        boundary = source.isascii() and source[0].isalnum() and source[-1].isalnum()
        patterns.append((r"(?<!\w)" if boundary else "") + re.escape(source) + (r"(?!\w)" if boundary else ""))
    pattern = re.compile("|".join(patterns), re.IGNORECASE)
    parts, start, matched = [], 0, False
    for match in pattern.finditer(text):
        parts.extend([escape(text[start:match.start()]), "<blbterm>" + escape(lookup[match.group().casefold()]) + "</blbterm>"])
        start, matched = match.end(), True
    if not matched:
        return text, False
    parts.append(escape(text[start:]))
    return "<blb>" + "".join(parts) + "</blb>", True


def unprotect(text):
    # Fail visibly if the provider damages the XML instead of speaking markup.
    root = ET.fromstring(text)
    return "".join(root.itertext())
