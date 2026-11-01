import re

SLANG_DICTIONARY = {
    r"\bgak\b": "tidak",
    r"\bnggak\b": "tidak",
    r"\bndak\b": "tidak",
    r"\budah\b": "sudah",
    r"\bdah\b": "sudah",
    r"\bbanget\b": "sangat",
    r"\bgimana\b": "bagaimana",
    r"\bkayak\b": "seperti",
    r"\btuh\b": "itu",
    r"\bnih\b": "ini",
    r"\bbikin\b": "membuat",
    r"\bnge-bug\b": "mengalami kesalahan perangkat lunak",
    r"\bngebug\b": "mengalami kesalahan perangkat lunak",
    r"\btektokan\b": "interaksi bolak-balik",
    r"\bkalo\b": "kalau",
    r"\bsampe\b": "sampai",
    r"\baja\b": "saja",
    r"\bdong\b": "",
    r"\bsih\b": "",
    r"\bbiar\b": "agar",
    r"\bbuat\b": "untuk",
    r"\bgua\b": "saya",
    r"\bgue\b": "saya",
    r"\blu\b": "anda",
    r"\blo\b": "anda",
    r"\bmakasih\b": "terima kasih",
    r"\botw\b": "dalam perjalanan",
    r"\bmager\b": "malas bergerak",
    r"\bbaper\b": "bawa perasaan",
    r"\bmikir\b": "berpikir",
    r"\bnyari\b": "mencari",
    r"\bpake\b": "pakai",
    r"\bngomong\b": "bicara",
    r"\bcuma\b": "hanya",
    r"\bcuman\b": "hanya",
}

COURSE_GLOSSARY = {
    "spanning tree": "Spanning Tree",
    "trunk": "Trunk",
    "etherchannel": "EtherChannel",
    "vlan": "VLAN",
    "subnetting": "Subnetting",
    "router": "Router",
    "switch": "Switch",
    "handshake": "Handshake",
    "payload": "Payload",
    "latency": "Latency",
    "jitter": "Jitter",
    "throughput": "Throughput",
    "bandwidth": "Bandwidth",
}

def normalize_indonesian_slang(text: str) -> str:
    if not text:
        return text
    normalized = text
    for pattern, replacement in SLANG_DICTIONARY.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized

def apply_course_glossary(text: str) -> str:
    if not text:
        return text
    result = text
    for term_lower, canonical in COURSE_GLOSSARY.items():
        pattern = re.compile(re.escape(term_lower), re.IGNORECASE)
        result = pattern.sub(canonical, result)
    return result

def preprocess_text(text: str) -> str:
    text = normalize_indonesian_slang(text)
    text = apply_course_glossary(text)
    return text
