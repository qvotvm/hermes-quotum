# quotum for Hermes Agent

A model provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) with a default seat mode and an optional prepaid Pay as you go (PAYG) mode. A seat is a Venice API key with a daily dollar cap. In seat mode, Hermes uses the key for private and anonymized Venice models, and `/usage` shows what the seat has spent this session, what is left and when the 20:00 UTC bell resets it.

This is a community plugin, not affiliated with Nous Research.

## Install

Requires Hermes 0.21.5 or later with `agent.secret_scope.get_secret`. Hosts without that API can load the plugin, but provider actions refuse credentials and the dashboard asks you to upgrade Hermes and restart it and its gateway. No Hermes yet? Install it first with the [Hermes Agent installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation).

Then the plugin:

```
hermes plugins install qvotvm/hermes-quotum --enable
```

Put the key from [quotum.org/seat](https://quotum.org/seat) in `~/.hermes/.env`:

```
QUOTUM_SEAT_KEY=...
```

Then pick the provider for a run, or make it the default:

```
hermes --provider quotum -m kimi-k3
hermes config set model.provider quotum
hermes config set model.default kimi-k3
```

If you run the gateway, restart it after installing: `hermes gateway restart`.

On a Hermes profile, run each command with `hermes -p <name>` and put the key in `~/.hermes/profiles/<name>/.env` instead of `~/.hermes/.env`. Plugins are installed and turned on for each profile separately.

The key can be a seat key, the seat's agent key or a day key. An agent key is cut from your seat on [quotum.org/seat](https://quotum.org/seat) with its own amount per session, so a busy agent can't spend the rest of the seat. Each session it gets that amount again; what it leaves unspent burns at the bell with the rest of the seat.

## PAYG (prepaid project keys)

Prepaid project-key intake is open in the 6 November capture. Enroll with an invite or an eligible funding wallet at [quotum.org/payg](https://quotum.org/payg), fund your project and create a project API key. Check the live intake status there before funding; per-request x402 intake remains closed. Put it in `~/.hermes/.env` (or your profile's `.env`):

```
QUOTUM_PAYG_KEY=...
```

The PAYG key takes precedence when both keys are set. Run `hermes --provider quotum -m openai-gpt-oss-120b`, choosing a text model allowed by your project key from the [PAYG catalog](https://quotum.org/api/payg/v1/models). Remove `QUOTUM_PAYG_KEY` to return to seat mode. Restart Hermes and the gateway after changing modes.

PAYG chat goes to `https://quotum.org/api/payg/v1/chat/completions` with your project bearer. `/usage` shows available balance, holds and spending; the dashboard's **Seals** tab shows balance and the latest 100 project receipts, including final price, refunds and the reported burn share of settled net margin. Use **Refresh balance and receipts** to update them. Pending prices stay pending. PAYG balance does not reset at the seat bell. Text chat defaults to the captured catalog's output limit of 2048 tokens and project/model limits; larger explicit output limits are rejected by the gateway. `QUOTUM_X_SEARCH` applies only to seat mode.

Manage wallet enrollment, funding and withdrawals on `/payg`. SDK retries are disabled for PAYG, and each chat request carries an idempotency key.

## What goes where

| request | host | what it carries |
|---|---|---|
| seat chat completions | api.venice.ai | your prompts, the model's replies and the key, straight from your machine to Venice |
| model list and optional preflight prices | quotum.org/api/seat/models | nothing of yours: the public catalog of the seat's models |
| `/usage`, 402 reset guidance and optional preflight | quotum.org/api/meter/&lt;h&gt; | h, the first 16 hex characters of the key's sha256, and the plugin version in a header. The key itself is never sent to quotum.org |
| X search model list (only with `QUOTUM_X_SEARCH=1`) | api.venice.ai/api/v1/models | nothing of yours: Venice's public model catalog, read once to see which models support X search |
| the Seals tab in `hermes dashboard` | quotum.org/api/meter/&lt;h&gt;, then quotum.org/api/record?seat=N | the same h as `/usage`, which answers the seat number; then the seat's public record by its number. The tab never sends the seat key |
| PAYG chat completions | quotum.org/api/payg/v1/chat/completions | your prompts and project bearer; quotum relays the request to its provider and returns the reply and billing receipt |
| PAYG model list and intake status | quotum.org/api/payg/v1/models, /status | no key; public catalog and intake status |
| PAYG `/usage` and the dashboard | quotum.org/api/payg/v1/meter | the project bearer; balance and recent receipts. Individual receipts are available from `/api/payg/v1/receipts/<id>` with that bearer. The dashboard browser receives balance and receipt metadata, never the bearer |

In seat mode, quotum never sees a prompt or a reply. The keeper reads only the dollar amount Venice reports for each key and publishes it per seat in the daily receipt. In PAYG mode, quotum's gateway processes prompts and replies; receipts contain billing metadata, not prompt or reply text.

There is no telemetry, no wallet code and no self-update. The seat meter is read for `/usage`, after a 402, and before inference only when preflight is enabled. Those provider reads share a cache per key hash, including failures, for five minutes by default. Seat catalog prices use the same interval. Dashboard seat records retain their separate 120-second cache; PAYG balance and receipts are fetched fresh on usage or dashboard refresh. Metadata requests do not follow redirects. Both inference clients restrict destinations to their mode's HTTPS host, disable environment proxy routing and ignore supplied custom HTTP clients. Credentials come only through Hermes's active profile scope via `agent.secret_scope.get_secret`. There is no process environment fallback. If the API or its accessor is absent, imports still succeed, provider actions raise a clear upgrade error before making a request, and the dashboard returns the same local guidance. Failures inside the API remain errors. Hermes reads the setup files described above; the plugin never reads credential files.

## Seals in Hermes

From 0.2.0 the plugin adds a **Seals** tab to `hermes dashboard`, next to Analytics. It shows your seat's record: seals from
Copper to Olympian for holding through bells, using the seat, sealed calls and burns, each counted from chain logs and bell
receipts. Secret seals show a lock and `???` until held. The record of every wallet is rebuilt and put on chain as one merkle root
after each bell, and anyone can recompute it (`node verify.mjs --record <address>`, see quotum.org/docs). Set `QUOTUM_WALLET`
in `~/.hermes/.env` to read another wallet's record instead of your seat's.

Hermes' bundled Achievements page currently has no hook for outside achievements, so seals remain in the **Seals** tab.
The cards use its Copper → Silver → Gold → Diamond → Olympian ladder and visual language, keeping quotum's own seal names
and awarded tiers, with engraved tier icons (unheld seals are dimmed, secrets stay locked). Enabling `quotum` also enables the Seals tab;
there is no separate dashboard plugin to enable. Each card explains its count and links to `quotum.org/record?w=<wallet>` to verify against the record
root on Robinhood Chain. This is compatibility with that record, not a rename or a local Hermes unlock. Successful records
are cached in memory for 120 seconds per selected identity; errors are retried on the next request. Nothing is written on chain.

## Models

The list is the seat's catalog: private models and anonymized models, as Venice marks them. With a PAYG key set, it comes from the separate PAYG text-chat catalog. Model access and availability are enforced by the gateway and your project key.

- **Private:** Venice keeps no prompt or reply.
- **Anonymized:** these are third-party models (for example Claude, GPT or Gemini). The provider behind the model sees the prompt but not who sent it.

Each model's class is shown on [quotum.org/models](https://quotum.org/models). If you only want private models, pick from that list. Seat requests turn off Venice's own system prompt; the PAYG gateway controls its own provider parameters.

## Live X search (optional)

Set `QUOTUM_X_SEARCH=1` in `~/.hermes/.env` and Grok models (grok-4-7 and the rest of the family) search the live web and X while they answer. On those requests Hermes' own web_search tool is left out, so the model searches through Venice instead (with it in, Venice refuses the request). Venice charges this per search, about $0.01 each, on top of the model's price, and it comes out of the same seat cap. Other models ignore the setting. It is off by default.

## Low cap warning (optional)

Set these in your Hermes profile's `.env`:

```
QUOTUM_PREFLIGHT=1
QUOTUM_PREFLIGHT_MINUTES=5
```

set `QUOTUM_METER_HANDLE` to exactly 16 lowercase hex characters (the first 16 of your seat key's sha256) behind a secrets proxy; invalid values are ignored with one warning, and a proxy key without a valid handle leaves inference running with local meter setup guidance.

Preflight is off by default and applies only to seat mode. In PAYG mode it performs no seat meter or price reads and never blocks chat. Before a chat request, it compares the meter's `leftUsd` with the chosen model's catalog price for a defined turn: 10,000 input tokens plus 1,000 output tokens. If less is left, it logs one warning per Hermes session and names the cheapest available private model with a lower price for that same turn. It keeps your chosen model.

The comparison is an estimate, not a quote or a reservation. It excludes search charges and does not read your prompt to estimate tokens. Missing prices, an unavailable meter or catalog, or no cheaper private model leave the request running without a warning. Calls without a Hermes session ID skip preflight. The warning record and caches live in memory; restarting Hermes clears them.

`QUOTUM_PREFLIGHT_MINUTES` controls the shared meter and catalog cache interval even when preflight is off. The default is 5 minutes, the minimum is 1; invalid values use 5. Preflight hashes the key on the actual inference request, including a pooled key, rather than assuming the environment key is the one in use. Session IDs, model choices and prompts never go to the meter.

## Errors

In PAYG mode, errors give one action at [quotum.org/payg](https://quotum.org/payg):

- **401:** the project key is missing, invalid, expired or revoked; create a new project key.
- **402 session limit:** raise the session limit. Other 402 balance errors ask you to top up the project.
- **403 access:** check project access or redeem an invite. Model-scope restrictions ask you to choose an allowed model.
- **503 paused intake:** check status and wait for intake to reopen.

PAYG 401, 402, 403 and 503 verdicts are non-retryable with credential rotation and fallback hints off. The PAYG SDK makes no automatic retries.

In seat mode:

- **402:** the key's cap is spent. The error gives the next 20:00 UTC bell's date and time, from the cached meter when available, otherwise computed in UTC. The verdict is non-retryable with credential rotation and fallback hints off. The OpenAI client refuses SDK retries of this response.
- **401:** the key was rotated or ended, or its seat was freed. The error links to [quotum.org/seat](https://quotum.org/seat) to get the key again, with the same terminal hints and SDK retry refusal.

Other seat errors retain SDK retry behavior and return a fixed message with the HTTP status. Redirects, HTTP and stream error bodies, and diagnostic response headers are sanitized before the SDK handles them. Transport and body-read failures use fixed diagnostic text. A request-local trace keeps HTTP trace event names while removing raw headers and exception details; caller-supplied trace callbacks are replaced. Successful chat responses use an explicit field schema for messages, tool calls, token usage and the supported `quotum_billing` receipt fields. Unknown extensions and SSE control fields are discarded. Malformed responses use a local error envelope. Streaming text and tool arguments hold only a possible credential prefix until the next fragment resolves it. Usage and dashboard responses use typed field allowlists; model labels accept bounded identifiers without control characters. The actual request key and both keys from the active profile are redacted before output. Decoder and close failures use local diagnostics while preserving known HTTP statuses. On the inspected Hermes build, its credential pool and configured fallback chain can run before the terminal retry check, independently of those hints. Another configured key or provider may still be tried. An ordinary single-key run stops on the first 402. The plugin does not install a timer, block a later user request, or change Hermes's pool cooldown to the bell. Older builds without the provider classification hook cannot guarantee the Hermes-level stop.

Both modes supply their own clients through `create_client` and require `plugins.isolation: in_process` (the default). Host isolation is unsupported.

## About quotum

Seats are paid for by the trading tax of the QUOTUM token on Robinhood Chain. What a seat leaves unspent at the bell buys QUOTUM and burns it. You don't need the token to read this code; you need a seat's key to use it. Nothing in this plugin buys, sells or signs anything.

## Local verification

Use an existing Python environment with Hermes, OpenAI SDK 2.24.0, httpx 0.28.1 and FastAPI. Run from this folder:

```
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -v
node tests/dashboard-smoke.cjs
```

Set `HERMES_SOURCE` if the read-only Hermes source is elsewhere. Tests use the real Hermes provider API, mocked HTTP transports and synthetic keys. No paid calls or live credentials are needed. Test dependencies are listed in `tests/requirements.txt`.

## License

MIT
