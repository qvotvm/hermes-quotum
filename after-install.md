# quotum provider installed

Put your seat's key in `~/.hermes/.env`:

    QUOTUM_SEAT_KEY=...

Then run it, or make it the default:

    hermes --provider quotum -m kimi-k3
    hermes config set model.provider quotum
    hermes config set model.default kimi-k3

Get the key at <https://quotum.org/seat>. `/usage` shows the session's spend, what is left and the 20:00 UTC bell. If you run the gateway: `hermes gateway restart`.
