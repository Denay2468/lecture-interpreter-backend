"""Conservative guards for early cuts; actual pauses/hard limits still flush."""
import re

# These words generally need a complement. Do not discard or rewrite them.
_OPEN_ENDINGS = frozenset('''tidak bukan belum jangan sangat terlalu dan atau
yang di ke dari nggak enggak ngga gak ga ndak tanpa dengan karena kalau kalo
agar supaya lebih kurang not no never without very the a an and or to of
because if than muy de el la'''.split())


def incomplete_phrase(text, glossary=()):
    words = re.findall(r"\w+", text.casefold())
    if not words:
        return False
    if words[-1] in _OPEN_ENDINGS:
        return True
    # Consider both lecture and question directions. An exact completed term
    # is fine, but a suffix such as 'jaringan' before 'saraf' should wait.
    for pair in glossary:
        for term in pair:
            tokens = re.findall(r"\w+", term.casefold())
            for count in range(1, min(len(tokens), len(words) + 1)):
                if words[-count:] == tokens[:count]:
                    return True
    return False
