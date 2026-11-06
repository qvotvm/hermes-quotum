// Offline host-SDK smoke test for the actual dashboard bundle.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../dashboard/dist/index.js"), "utf8");

async function mount(responses) {
  let component, cursor = 0, effectDeps, effect, calls = 0;
  const state = [];
  const React = {
    Fragment: "fragment",
    createElement: (type, props, ...children) => typeof type === "function" ? type({ ...props, children }) : { type, props: props || {}, children },
    useState: initial => {
      const index = cursor++;
      if (!(index in state)) state[index] = initial;
      return [state[index], value => { state[index] = typeof value === "function" ? value(state[index]) : value; }];
    },
    useEffect: (fn, deps) => {
      if (!effectDeps || deps.some((d, i) => d !== effectDeps[i])) { effect = fn; effectDeps = deps; }
    }
  };
  const window = {
    __HERMES_PLUGIN_SDK__: { React, fetchJSON: async path => {
      assert.equal(path, "/api/plugins/quotum/account");
      const result = responses[Math.min(calls++, responses.length - 1)];
      if (result instanceof Error) throw result;
      return structuredClone(result);
    } },
    __HERMES_PLUGINS__: { register: (name, page) => { assert.equal(name, "quotum"); component = page; } }
  };
  vm.runInNewContext(source, { window, Set, Number, String, Object, Math, encodeURIComponent });
  function render() { cursor = 0; return component(); }
  async function settle() {
    let tree = render();
    if (effect) { const fn = effect; effect = null; fn(); }
    await new Promise(resolve => setImmediate(resolve));
    return render();
  }
  return { settle, calls: () => calls };
}

function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...tree.children.flatMap(nodes)];
}
function content(tree) {
  if (Array.isArray(tree)) return tree.map(content).join(" ");
  if (tree === null || tree === undefined) return "";
  return typeof tree === "object" ? tree.children.map(content).join(" ") : String(tree);
}

(async () => {
  const payg = { mode: "payg", project: "fixture-project", balance: { availableUsd: 1.75, totalUsd: 2, heldUsd: .2, refundPendingUsd: .05, spentUsd: 1 },
    notice: "PAYG intake is paused. Check quotum.org/payg and wait for intake to reopen.", receipts: [
      { id: "finished", model: "openai-gpt-6-luna", status: "charged", chargeUsd: .0000375, refundUsd: 0, burnShareUsd: .000003825, tokensIn: 20, tokensOut: 10 },
      { id: "pending", model: "openai-gpt-6-luna", status: "pending-reconcile", chargeUsd: null, refundUsd: 0, burnShareUsd: null, tokensIn: null, tokensOut: null }
    ] };
  const host = await mount([payg, { ...payg, balance: { ...payg.balance, availableUsd: 1.5 } }]);
  let tree = await host.settle();
  let text = content(tree);
  for (const expected of ["Available balance", "Prepaid project keys", "$1.750000", "Recent receipts", "finished", "Pending", "paused", "20 / 10"]) assert.ok(text.includes(expected), expected);
  assert.ok(!text.toLowerCase().includes("credit"));
  nodes(tree).find(n => n.type === "button").props.onClick();
  tree = await host.settle();
  assert.ok(content(tree).includes("$1.500000"));
  assert.equal(host.calls(), 2);
  console.log("PASS PAYG balance, receipts, pending prices, pause and refresh");

  const empty = await mount([{ ...payg, receipts: [] }]);
  assert.ok(content(await empty.settle()).includes("No receipts yet."));
  console.log("PASS PAYG empty receipt state");

  const seat = await mount([{ seat: 3, wallet: "fixture-wallet", record: { seals: { used: 1 }, counts: { used: 100 } }, catalog: [
    { id: "used", name: "Used", family: "seat", tiers: [10, 100, 1000, 10000, 100000] }
  ] }]);
  text = content(await seat.settle());
  assert.ok(text.includes("Seat 3"));
  assert.ok(text.includes("Used"));
  assert.ok(!text.includes("Pay as you go"));
  console.log("PASS seat seals render unchanged");

  const retry = await mount([{ mode: "payg", error: "PAYG invite access required. Request access at quotum.org/payg." }, payg]);
  tree = await retry.settle();
  assert.ok(content(tree).includes("Request access"));
  assert.equal(nodes(tree).find(n => n.props.role === "alert").type, "p");
  nodes(tree).find(n => n.type === "button").props.onClick();
  assert.ok(content(await retry.settle()).includes("Available balance"));
  console.log("PASS access error and retry recovery");

  const setup = await mount([{ error: "set QUOTUM_SEAT_KEY (your seat key or Hermes key) or QUOTUM_WALLET in ~/.hermes/.env." }]);
  tree = await setup.settle();
  assert.ok(content(tree).includes("QUOTUM_SEAT_KEY"));
  assert.equal(nodes(tree).find(n => n.props.role === "alert").type, "p");
  assert.ok(nodes(tree).some(n => n.type === "button" && content(n) === "Retry"));
  console.log("PASS no-key setup state and retry control");

  const failure = await mount([new Error("fixture-project-key"), payg]);
  tree = await failure.settle();
  assert.ok(!content(tree).includes("fixture-project-key"));
  assert.ok(content(tree).includes("did not answer"));
  nodes(tree).find(n => n.type === "button").props.onClick();
  assert.ok(content(await failure.settle()).includes("Available balance"));
  console.log("PASS rejected host fetch uses a fixed message and recovers");
})().catch(error => { console.error(error); process.exitCode = 1; });
