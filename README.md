# quotum for Hermes Agent

A model provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that runs on a quotum seat's key. A seat is a Venice API key with a daily dollar cap. Hermes uses the key for private and anonymized Venice models, and `/usage` shows what the seat has spent this session, what is left and when the 20:00 UTC bell resets it.

This is a community plugin, not affiliated with Nous Research.

## Install

Requires Hermes 0.21.5 or later. No Hermes yet? Install it first.

```
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

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

## What goes where

| request | host | what it carries |
|---|---|---|
| chat completions | api.venice.ai | your prompts, the model's replies and the key, straight from your machine to Venice |
| model list | quotum.org/api/seat/models | nothing of yours: the public catalog of the seat's models |
| `/usage` | quotum.org/api/meter/&lt;h&gt; | h, the first 16 hex characters of the key's sha256, and the plugin version in a header. The key itself is never sent to quotum.org |
| X search model list (only with `QUOTUM_X_SEARCH=1`) | api.venice.ai/api/v1/models | nothing of yours: Venice's public model catalog, read once to see which models support X search |
| the Seals tab in `hermes dashboard` | quotum.org/api/meter/&lt;h&gt;, then quotum.org/api/record?seat=N | the same h as `/usage`, which answers the seat number; then the seat's public record by its number. The tab never sends the key |

quotum never sees a prompt or a reply. The keeper reads only the dollar amount Venice reports for each key and publishes it per seat in the daily receipt.

There is no telemetry, no wallet code and no self-update. Meter calls happen when Hermes asks for account usage or when the Seals tab resolves a seat.

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

The list is the seat's catalog: private models and anonymized models, as Venice marks them.

- **Private:** Venice keeps no prompt or reply.
- **Anonymized:** these are third-party models (for example Claude, GPT or Gemini). The provider behind the model sees the prompt but not who sent it.

Each model's class is shown on [quotum.org/models](https://quotum.org/models). If you only want private models, pick from that list. The plugin turns off Venice's own system prompt on every request.

## Live X search (optional)

Set `QUOTUM_X_SEARCH=1` in `~/.hermes/.env` and Grok models (grok-4-7 and the rest of the family) search the live web and X while they answer. On those requests Hermes' own web_search tool is left out, so the model searches through Venice instead (with it in, Venice refuses the request). Venice charges this per search, about $0.01 each, on top of the model's price, and it comes out of the same seat cap. Other models ignore the setting. It is off by default.

## Errors

- **402:** the key's cap for this session is spent. It comes back at the 20:00 UTC bell.
- **401:** the key was rotated or ended, or its seat was freed. Get the key again on quotum.org/seat.

## About quotum

Seats are paid for by the trading tax of the QUOTUM token on Robinhood Chain. What a seat leaves unspent at the bell buys QUOTUM and burns it. You don't need the token to read this code; you need a seat's key to use it. Nothing in this plugin buys, sells or signs anything.

## License

MIT
