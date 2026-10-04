# quotum for Hermes Agent

A model provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that runs on a quotum seat's key. A seat is a Venice API key with a daily dollar cap. Hermes uses the key for private and anonymized Venice models, and `/usage` shows what the seat has spent this session, what is left and when the 20:00 UTC bell resets it.

## Install

Requires Hermes 0.21.5 or later.

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

The key can be a seat key or a day key.

## What goes where

| request | host | what it carries |
|---|---|---|
| chat completions | api.venice.ai | your prompts, the model's replies and the key, straight from your machine to Venice |
| model list | quotum.org/api/seat/models | nothing of yours: the public catalog of the seat's models |
| `/usage` | quotum.org/api/meter/&lt;h&gt; | h, the first 16 hex characters of the key's sha256, and the plugin version in a header. The key itself is never sent to quotum.org |
| X search model list (only with `QUOTUM_X_SEARCH=1`) | api.venice.ai/api/v1/models | nothing of yours: Venice's public model catalog, read once to see which models support X search |

quotum never sees a prompt or a reply. The keeper reads only the dollar amount Venice reports for each key and publishes it per seat in the daily receipt.

There is no telemetry, no wallet code and no self-update. The meter call happens only when Hermes asks for account usage.

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
