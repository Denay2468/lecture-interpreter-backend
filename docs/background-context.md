# Background lecture context

The lecturer UI now asks for room, language and optional PDF modules. Department,
course presets, terminology review and manual overrides are removed from this UI.
The catalog and legacy server configuration remain for backwards compatibility,
but the new browser does not send a preset or manual glossary. PDF uploads no
longer force catalog replacements for the entire lecture. Relevant PDF passages
and recent speech remain translation hints.

## Optional AI service

A per-lecture background worker can call an OpenAI-compatible chat completions
endpoint to propose a short topic and factual summary. It does not translate the
speech or generate terminology overrides. The next lecture phrase uses a frozen
snapshot of the latest relevant summary alongside existing context. Workers and
retries use the same captured snapshot. Student questions retain their existing
PDF context path and do not update the lecturer's topic summary.

This release implements the integration, not a bundled local model or free cloud
service. It is disabled by default: the operator must choose a compatible model
and configure its endpoint. This avoids unexpected billing and disclosure of
transcripts to a new service. No lecturer-facing configuration is required.

Add to the root .env, using real provider settings rather than these placeholders:

```dotenv
CONTEXT_AGENT_ENABLED=true
CONTEXT_AGENT_URL=https://YOUR_PROVIDER/YOUR_CHAT_COMPLETIONS_PATH
CONTEXT_AGENT_MODEL=YOUR_MODEL_NAME
CONTEXT_AGENT_API_KEY=YOUR_KEY
CONTEXT_AGENT_INTERVAL_S=15
CONTEXT_AGENT_TIMEOUT_S=2
CONTEXT_AGENT_MAX_AGE_S=15
CONTEXT_AGENT_MIN_CONFIDENCE=0.8
```

The URL is the full chat completions URL, not just a base URL. Remote providers
require HTTPS. A compatible local service may use HTTP on localhost with an empty
key. The model must follow a JSON-only instruction and return standard choices /
message / content responses. Providers requiring other request formats need an
adapter. Existing Deepgram, DeepL and ElevenLabs keys cannot automatically serve
as this additional model's credentials. Restart the backend after configuration.
Set CONTEXT_AGENT_ENABLED=false to disable extra calls.

## Bounds and failure behavior

- One task and one pending item per lecture; newer pending speech replaces older
  analysis work, never the speech sent for translation.
- Default interval 15 seconds between request starts: approximately 240 requests
  per continuously active class-hour at most. Actual usage depends on speech and
  failures. This is not a currency budget; provider charges depend on tokens and
  pricing. Increase the interval to reduce call frequency.
- Inputs: at most 1,500 recent transcript characters and two relevant PDF passages.
  Output token request limit 300; response bytes capped at 16 KiB. Topic/summary
  length limits 120/600 characters.
- A two-second timeout, no immediate retry, and no waiting in the translation
  enqueue path. Failed, malformed or low-confidence results revert to existing
  speech/PDF context. Old summaries expire and lexical relevance is checked.
- Confidence is model-reported, not a calibrated probability of correctness.
  The lexical gate is conservative and cannot detect every topic change.
- Closing a lecture cancels the worker, closes its HTTP pool and clears memory.
  No transcript, provider response or secret is logged by this component.

PDFs and transcripts are treated as data in the prompt. Generated summaries are
bounded hints, not commands or authoritative corrections. This reduces exposure
but cannot guarantee that a model never follows malicious text or invents details.
Operator-selected endpoints receive transcript and module excerpts when enabled.

## Validation and limits

Offline tests cover latest-only coalescing, cancellation, timeout, malformed and
low-confidence replies, expiry, classroom isolation, frozen enqueue context,
provider request shape, response limits, and the simplified lecturer UI.
No live provider quality or classroom latency improvement has been measured.
Background network/CPU work still consumes resources; zero latency impact cannot
be guaranteed. Sudden topic changes may precede a useful summary by several
phrases. Test with recordings and compare median/p95 latency and terminology
errors with the feature enabled and disabled before relying on it in a demo.

```powershell
python -m unittest tools.test_context_agent tools.test_translation_accuracy tools.test_materials -v
```
