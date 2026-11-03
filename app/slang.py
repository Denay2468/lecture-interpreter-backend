"""Conservative Indonesian normalization for MT, never rewriting displayed STT."""
import re

WORDS = {
    "nggak": "tidak", "ngga": "tidak", "gak": "tidak", "ga": "tidak",
    "enggak": "tidak", "gimana": "bagaimana", "gitu": "begitu",
    "gini": "begini", "udah": "sudah", "belom": "belum", "belum": "belum",
    "emang": "memang", "banget": "sekali", "pengen": "ingin", "pengin": "ingin",
    "bikin": "membuat", "dapet": "mendapat", "denger": "mendengar",
    "ngerti": "mengerti", "ngomong": "berbicara", "ngeliat": "melihat",
    "liat": "lihat", "nyoba": "mencoba", "nanya": "bertanya",
    "ndak": "tidak", "kalo": "kalau", "sampe": "sampai",
    "aja": "saja", "makasih": "terima kasih", "mikir": "berpikir",
    "nyari": "mencari", "pake": "pakai", "cuman": "hanya",
    "ngebug": "mengalami kesalahan perangkat lunak",
    "gua": "saya", "gue": "saya", "lu": "kamu", "lo": "kamu",
}
_PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")(?!\w)", re.I)


def normalize_indonesian(text):
    # Keep acronyms (e.g. GA), punctuation, English code-switching and unknown words.
    def replace(match):
        word = match.group()
        if word.isupper():
            return word
        replacement = WORDS[word.lower()]
        return replacement.capitalize() if word[0].isupper() else replacement
    return _PATTERN.sub(replace, text)
