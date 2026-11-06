# quotum provider installed

Put your seat's key in `~/.hermes/.env`:

    QUOTUM_SEAT_KEY=...

Then run it, or make it the default:

    hermes --provider quotum -m kimi-k3
    hermes config set model.provider quotum
    hermes config set model.default kimi-k3

Get the key at <https://quotum.org/seat>. `/usage` shows the session's spend, what is left and the 20:00 UTC bell. If you run the gateway: `hermes gateway restart`.

## Pay as you go (prepaid project keys)

Enroll with an invite or an eligible funding wallet at <https://quotum.org/payg>, fund your project and create a project API key. Set it in `~/.hermes/.env`:

    QUOTUM_PAYG_KEY=...

Run `hermes --provider quotum -m openai-gpt-oss-120b`, choosing an allowed text model from <https://quotum.org/api/payg/v1/models>. The PAYG key takes precedence over the seat key; remove it to return to seat mode. Restart Hermes and the gateway after changing modes. Chat goes through quotum's PAYG gateway. `/usage` and the dashboard's **Seals** tab show balance and recent receipts. `QUOTUM_X_SEARCH` applies only to seat mode.

Both modes require `plugins.isolation: in_process` (the default). The manifest leaves `requires_env` empty because Hermes has no either/or contract: configure one key, not both. Missing seat keys name only the seat key variable; PAYG errors link to the project console.

Seat 401/402 errors stop SDK retries and include key recovery or the next 20:00 UTC bell. PAYG 401/402 errors include project-key, session-limit or balance guidance at <https://quotum.org/payg>. Hermes pools or configured fallbacks can run before terminal hints.

For an optional seat-only low-cap warning, set `QUOTUM_PREFLIGHT=1`. It estimates 10k input plus 1k output tokens and suggests a cheaper private model once per session without changing your model or blocking a call. `QUOTUM_PREFLIGHT_MINUTES=5` sets the shared seat meter/catalog cache interval, minimum 1 minute. PAYG skips this seat check. Metadata redirects are refused; inference uses the mode's fixed HTTPS host without environment proxies or custom HTTP clients. Keys come only through Hermes's profile scope (`agent.secret_scope.get_secret`); Hermes reads the setup files above. The plugin never reads credential files or falls back to process environment keys. If the API is absent, the plugin still loads, but provider actions and the dashboard ask you to upgrade Hermes and restart it and its gateway.
