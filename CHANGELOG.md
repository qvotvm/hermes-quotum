# Changelog

## 0.3.0 (2026-11-06)

- Add prepaid PAYG project-key mode with precedence over the seat key, a separate text-chat catalog, 2048-token default output limit, authenticated balance and receipts in usage and the existing dashboard tab. Preserve JSON and SSE billing metadata, unique or caller-supplied idempotency keys, and zero PAYG SDK retries.
- Make seat 401/402 errors non-retryable with credential rotation and fallback hints off. Explain key recovery or the next 20:00 UTC bell using the actual inference key's hash and safe UTC fallback. Preserve transient seat SDK retries.
- Use PAYG-specific key, session-limit and balance guidance at quotum.org/payg. Support the captured nested error envelope and invalid-key 401 response. Keep PAYG balance separate from the seat bell.
- Add opt-in seat preflight warnings for a defined 10k input plus 1k output turn, once per session, suggesting a cheaper available private model without switching or blocking. PAYG skips seat preflight.
- Share seat meter successes and failures across usage, 402 guidance and preflight, plus cached catalog prices. QUOTUM_PREFLIGHT_MINUTES defaults to 5, minimum 1.
- Refuse metadata redirects and restrict inference to each mode's HTTPS host; disable environment proxies and supplied custom clients. Resolve credentials only through Hermes's active profile scope, without process environment or credential-file fallbacks. Keep imports safe when the API is absent; provider actions and the dashboard report a clear upgrade error.
- Document either/or key setup, prepaid intake, data routing, in-process isolation, and limits of Hermes credential-pool and fallback hints. Add offline merge and captured-contract regressions.
- Sanitize upstream HTTP and stream errors, diagnostic headers and dashboard error payloads. Preserve retry controls for transient seat failures. Fail closed when the credential-scope module cannot initialize.
- Cover redirects, successful-response headers, HTTP trace diagnostics, error envelopes with HTTP 200 and streams with missing content types. Keep PAYG 401/402 terminal when their body cannot be read. Redact the active credential from usage and dashboard metadata and preflight warnings; validate PAYG presentation fields.
- Restrict successful chat, seat metadata and model labels to explicit output schemas. Preserve supported chat text, tools, usage and receipts; discard unknown diagnostic fields. Redact both profile keys and the actual request key, including streamed text split across deltas. Keep decoder and close failures private and dashboard fetch failures readable.
