(function () {
  "use strict";
  // quotum Seals: the seat's record from quotum.org (oracle/SPEC-RECORD-v0.md). MIT. No build step: plain React from the host SDK.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React, h = React.createElement, useState = React.useState, useEffect = React.useEffect;
  const TIERS = ["Copper", "Silver", "Gold", "Diamond", "Olympian"];
  const ICONS = "/dashboard-plugins/quotum/dist/seals/";
  const SECRET_IDS = new Set(["witness", "same_block", "zero_left", "came_back", "within_a_bip", "guess_bip", "full_house"]);
  const FAMILY = { bell: "The bell", pyre: "The pyre", seat: "The seat", call: "The call", guess: "The guess", meta: "Rare" };
  const SCAN = "https://robin.etherscan.io/tx/";
  const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "");
  const unit = (id, n) => (id === "used" ? "$" + (n / 100).toFixed(2) : id === "pyre" ? n.toLocaleString("en-US") + " QUOTUM" : String(n));
  // Count meanings from SPEC-RECORD-v0; names, thresholds and awarded tiers come from the record API.
  const COUNTS = {
    bells_held: "Bells holding at least 250,000 QUOTUM.",
    held_through: "Consecutive bells holding at least 250,000 QUOTUM.",
    bell_one: "Held at least 250,000 QUOTUM at bell one.",
    witness: "Held through a bell burning at least 2% of the supply.",
    same_block: "Balance rose in the block of a bell.",
    pyre: "Own QUOTUM sent to the zero or dead address, in whole tokens.",
    pyre_days: "Bells in which the wallet burned QUOTUM.",
    seated: "Consecutive bells holding a seat; freeing the seat resets the run.",
    never_idle: "Consecutive bells with at least $0.10 spent in the seat receipt.",
    used: "Lifetime dollars spent across the seat's bell receipts.",
    agent_on_seat: "Bells with agent spending in the receipt.",
    zero_left: "A bell receipt spending at least 99% of the cap.",
    came_back: "Took a seat after freeing one for the same wallet.",
    calls_sealed: "Revealed calls of the wallet's seat, of any kind.",
    closest: "Call sessions tied for the closest seat call.",
    beat_house: "Call sessions beating ensemble-v0; Gold and above also require wins in at least 55% of the last 20 such sessions.",
    beat_open: "Call sessions beating a prediction that copies the open.",
    by_hand: "Wallet-signed calls, by hand or ask.",
    own_agent: "Calls sealed through the wallet's own agent (path C).",
    within_a_bip: "A call missing by at most one basis point.",
    guesses: "Revealed holder guesses with at least 250,000 QUOTUM held at the bell.",
    beat_bot: "Holder guesses beating ensemble-v0.",
    guess_bip: "A holder guess missing by at most one basis point.",
    full_house: "Gold or above in all five families."
  };

  function Ladder() {
    return h("div", { className: "qs-ladder", "aria-label": "Seal tier ladder" }, TIERS.map(function (tier, i) {
      return h(React.Fragment, { key: tier }, i ? h("span", { className: "qs-arrow", "aria-hidden": "true" }, "→") : null,
        h("span", { className: "qs-tier qs-tier-" + tier.toLowerCase() }, tier));
    }));
  }

  function Steps(props) {
    const cells = [];
    for (let i = 0; i < 10; i++) cells.push(h("i", { key: i, className: i < props.filled ? "f" : "" }));
    return h("div", { className: "qs-steps", "aria-hidden": "true" }, cells);
  }

  function Seal(props) {
    const s = props.seal, tier = props.tier || 0, n = props.count || 0;
    const hidden = s.secret && !tier;
    const icon = SECRET_IDS.has(s.id) ? "secret.webp" : s.id + "-" + TIERS[Math.max(0, tier - 1)].toLowerCase() + ".webp";
    const prev = tier > 0 ? s.tiers[tier - 1] : 0, next = s.tiers[tier];
    const filled = next === undefined ? 10 : Math.max(0, Math.min(10, Math.floor(((n - prev) / (next - prev)) * 10)));
    const line = next === undefined ? (s.tiers.length === 1 ? "Found" : "Top tier") : `${unit(s.id, n)} now, next tier at ${unit(s.id, next)}`;
    return h("div", { className: "qs-seal qs-tier-" + (tier ? TIERS[tier - 1].toLowerCase() : "pending"), "data-seal-id": "quotum." + s.id },
      h("div", { className: "qs-disc" + (tier ? "" : " qs-disc-pending"), "aria-hidden": "true" },
      hidden ? h("svg", { className: "qs-icon", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.5, "aria-hidden": "true" },
        h("rect", { x: 5, y: 10, width: 14, height: 11, rx: 2 }),
        h("path", { d: "M8 10V7a4 4 0 0 1 8 0v3M12 14v3" })) :
        h("img", { className: "qs-icon" + (tier ? "" : " qs-icon-pending"), src: ICONS + icon, alt: "", width: 128, height: 128 })),
      h("div", { className: "qs-badges" }, h("span", { className: "qs-tier" }, tier ? TIERS[tier - 1] : "Copper next"),
        h("span", { className: "qs-tier" }, tier ? "Unlocked" : hidden ? "Secret" : "Discovered")),
      h("b", null, hidden ? "???" : s.name),
      h("p", { className: "qs-description" }, hidden ? "A secret seal. Its name is revealed when held." : s.description || COUNTS[s.id] || "Counted from chain logs and bell receipts."),
      hidden ? null : h(Steps, { filled }),
      hidden ? null : h("small", null, line, props.rarity != null ? `. ${Math.round(props.rarity * 1000) / 10}% of records hold it` : ""),
      h("small", { className: "qs-verify" }, "verify against the record root on Robinhood Chain: ",
        h("a", { href: "https://quotum.org/record?w=" + encodeURIComponent(props.wallet), target: "_blank", rel: "noopener" }, "quotum.org/record?w=" + props.wallet)));
  }

  function SealsPage() {
    const [d, setD] = useState(null), [err, setErr] = useState(null);
    useEffect(function () {
      let active = true;
      SDK.fetchJSON("/api/plugins/quotum/record").then(function (j) { if (!active) return; if (j.error) setErr(j.error); else setD(j); }).catch(function (e) { if (active) setErr(String(e.message || e)); });
      return function () { active = false; };
    }, []);
    if (err) return h("div", { className: "qs" }, h("h1", null, "Seals"), h("p", { className: "qs-note" }, err));
    if (!d) return h("div", { className: "qs" }, h("h1", null, "Seals"), h("p", { className: "qs-note" }, "Reading the record…"));
    const rec = d.record, cat = d.catalog || [];
    const head = h("div", { className: "qs-head" },
      h("h1", null, d.seat ? `Seat ${d.seat}` : "Seals"),
      h("p", { className: "qs-note" }, short(d.wallet), d.session ? `, the record at bell ${d.session}` : "",
        d.rootTx ? h(React.Fragment, null, ", ", h("a", { href: SCAN + d.rootTx, target: "_blank", rel: "noopener" }, "the root on chain")) : null,
        ". Built from chain logs and bell receipts; ", h("a", { href: "https://quotum.org/record?w=" + encodeURIComponent(d.wallet), target: "_blank", rel: "noopener" }, "check it on quotum.org"), "."),
      h(Ladder), h("p", { className: "qs-note" }, "quotum names and tiers follow the on-chain record. Secret seals stay hidden until held."));
    if (!rec) return h("div", { className: "qs" }, head, h("p", { className: "qs-note" }, "No record for this wallet yet. Hold 250,000 QUOTUM through a bell, or seal a call."));
    const hidden = cat.filter(function (s) { return s.secret && !(rec.seals[s.id] > 0); }).length;
    const families = Object.keys(FAMILY).map(function (f) {
      const seals = cat.filter(function (s) { return s.family === f; });
      if (!seals.length) return null;
      return h("section", { key: f, className: "qs-fam" }, h("h2", null, FAMILY[f]),
        h("div", { className: "qs-grid" }, seals.map(function (s) { return h(Seal, { key: s.id, seal: s, tier: rec.seals[s.id], count: rec.counts[s.id], rarity: d.rarity && d.rarity[s.id], wallet: d.wallet }); })));
    });
    return h("div", { className: "qs" }, head, families, hidden ? h("p", { className: "qs-note" }, `${hidden} secret seals not found yet.`) : null,
      h("p", { className: "qs-note" }, "This is a community plugin, not affiliated with Nous Research."));
  }

  window.__HERMES_PLUGINS__.register("quotum", SealsPage);
})();
