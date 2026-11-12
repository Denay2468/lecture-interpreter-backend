"""Read-only IT course seeds shared with the lecturer UI; resolved per session."""
import json
from functools import lru_cache
from pathlib import Path
from .glossary import parse_glossary

CATALOG = Path(__file__).resolve().parent.parent / 'frontend' / 'data' / 'it-courses.json'


@lru_cache(maxsize=1)
def _profiles():
    data = json.loads(CATALOG.read_text(encoding='utf-8'))
    return {c['id']: (parse_glossary('\n'.join(f'{s} = {t}' for s, t in c['terms'])), c['context'])
            for c in data['courses']}


def resolve_course_glossary(profile_id, text, source_language):
    """Custom entries override seeds; retain original 40-entry/8000-char limits."""
    if not isinstance(profile_id, str):
        raise ValueError('Course profile must be text')
    custom = parse_glossary(text)
    if not profile_id:
        return custom, ''
    if source_language != 'id':
        raise ValueError('IT course profiles currently require Indonesian input')
    if profile_id not in _profiles():
        raise ValueError('Unknown IT course profile')
    pairs, context = _profiles()[profile_id]
    merged = {s.casefold(): (s, t) for s, t in pairs}
    merged.update({s.casefold(): (s, t) for s, t in custom})
    validated = parse_glossary('\n'.join(f'{s} = {t}' for s, t in merged.values()))
    return validated, context
