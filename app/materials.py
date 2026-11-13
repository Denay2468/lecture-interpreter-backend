"""Bounded, lecture-scoped PDF text retrieval and automatic terminology.

No document instructions are executed. Text is passed only as translation context.
"""
from dataclasses import dataclass
import re
from .course_profiles import _profiles

MAX_FILES = 10
MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT = 100_000
MAX_PAGES = 100
_COMMON = frozenset('the a an is are of to and or in on for this that it with why how '
                    'yang dan atau di ke dari ini itu adalah untuk dengan pada apa bagaimana'.split())


def tokens(text):
    return frozenset(re.findall(r"[^\W_]{2,}", text.casefold())) - _COMMON


@dataclass(frozen=True)
class Passage:
    text: str
    words: frozenset


def validate_materials(value):
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ValueError('Use at most 10 PDF modules.')
    documents = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('Invalid module data.')
        name, text = item.get('name'), item.get('text')
        if not isinstance(name, str) or not 1 <= len(name) <= 160:
            raise ValueError('Invalid module name.')
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT:
            raise ValueError('Each module must contain 1–100,000 extracted characters.')
        documents.append({'name': name, 'text': text})
    return documents


def index_materials(documents):
    passages = []
    for doc in documents:
        # Small overlapping windows also work when PDF line breaks are poor.
        text = doc['text']
        for start in range(0, len(text), 600):
            excerpt = text[start:start + 800]
            passages.append(Passage(excerpt, tokens(excerpt)))
    return tuple(passages)


def relevant_context(passages, text, history='', pairs=()):
    query = tokens(text)
    expanded = set(query)
    for source, target in pairs:
        if tokens(source) & query or tokens(target) & query:
            expanded.update(tokens(source)); expanded.update(tokens(target))
    prior = tokens(history[-500:])
    ranked = sorted(enumerate(passages),
                    key=lambda item: (len(item[1].words & expanded) * 4 + len(item[1].words & prior), -item[0]),
                    reverse=True)
    selected = []
    for _, passage in ranked:
        if not (passage.words & (expanded | prior)):
            continue
        selected.append(passage.text)
        if len(selected) == 2:
            break
    return '\n'.join(selected)


def automatic_terms(documents, language):
    """Activate attested catalog terms that appear in the uploaded material.

    Unknown subject terminology uses retrieved context, not guessed word swaps.
    Conflicting catalog definitions are omitted rather than silently selected.
    """
    if language != 'id':
        return ()
    combined = '\n'.join(d['text'] for d in documents)
    # Do not force IT senses of words like 'jaringan' in a medical module.
    # Other subjects still benefit from retrieved passages without hard swaps.
    if not re.search(r'\b(komputer|pemrograman|perangkat lunak|basis data|machine learning|computer|software|database)\b', combined, re.I):
        return ()
    mappings = {}
    catalog = {}
    for pairs, _ in _profiles().values():
        for source, target in pairs:
            catalog.setdefault(source.casefold(), set()).add((source, target))
    for key, choices in catalog.items():
        if key not in mappings and re.search(r'(?<!\w)' + re.escape(key) + r'(?!\w)', combined, re.I):
            mappings[key] = choices
    result = []
    for choices in mappings.values():
        targets = {target.casefold() for _, target in choices}
        if len(targets) == 1:
            result.append(sorted(choices)[0])
    return tuple(result[:80])
