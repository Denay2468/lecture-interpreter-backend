"""Compatibility helpers for existing scripts; live lectures use scoped glossary pairs."""
import re
from .slang import normalize_indonesian as normalize_indonesian_slang

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

def apply_course_glossary(text: str) -> str:
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in COURSE_GLOSSARY) + r")(?!\w)", re.I)
    return pattern.sub(lambda m: COURSE_GLOSSARY[m.group().lower()], text)


def preprocess_text(text: str) -> str:
    return apply_course_glossary(normalize_indonesian_slang(text))
