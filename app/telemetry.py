"""Session-local metrics helpers. Rates are supplied by the deployment, not guessed."""
import math


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered)-1, math.ceil(len(ordered)*percent/100)-1))]


def estimate_cost(elapsed_s, mt_characters, tts_characters, rates):
    """Wall-time STT approximation plus submitted characters, including retries.

    Excludes extra question STT, hosting, tax, free tiers and provider rounding.
    Returns None until all three rates are supplied in the same currency.
    """
    if any(rate is None for rate in rates):
        return None
    stt_per_minute, mt_per_million, tts_per_million = rates
    return elapsed_s/60*stt_per_minute + mt_characters/1e6*mt_per_million + tts_characters/1e6*tts_per_million
