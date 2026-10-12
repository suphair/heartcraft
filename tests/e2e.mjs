// Two players in headless Chrome tabs join a local server and play in one world.
// Run through tests/e2e.sh, which starts the server and the browser.
//
//   node tests/e2e.mjs <devtools port> <game url> [screenshot.png]
// The room password is "pw" (tests/e2e.sh) or HC_PASS.
const [, , port, base, shotFile] = process.argv;
const pass = process.env.HC_PASS || "pw";
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const browser = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise(r => (browser.onopen = r));

let seq = 0;
const pending = new Map();
const pageErrors = [];
browser.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.method === "Runtime.exceptionThrown") {
    pageErrors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  }
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m);
    pending.delete(m.id);
  }
};
const call = (method, params = {}, sessionId) => new Promise(r => {
  const id = ++seq;
  pending.set(id, r);
  browser.send(JSON.stringify({ id, method, params, sessionId }));
});

async function tab() {
  const { result: { targetId } } = await call("Target.createTarget", { url: "about:blank", newWindow: true });
  const { result: { sessionId } } = await call("Target.attachToTarget", { targetId, flatten: true });
  await call("Runtime.enable", {}, sessionId);
  await call("Page.enable", {}, sessionId);
  await call("Page.navigate", { url: base }, sessionId);
  const ev = async expr => {
    const r = await call("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sessionId);
    if (!r.result || r.result.exceptionDetails) {
      throw new Error(JSON.stringify(r.error || r.result.exceptionDetails));
    }
    return r.result.result.value;
  };
  ev.shot = async file => {
    await call("Page.bringToFront", {}, sessionId);
    await sleep(800);
    const r = await call("Page.captureScreenshot", { format: "png" }, sessionId);
    (await import("node:fs")).writeFileSync(file, Buffer.from(r.result.data, "base64"));
  };
  ev.loaded = async () => {
    for (let t = Date.now(); Date.now() - t < 20000; await sleep(200)) {
      const r = await call("Runtime.evaluate", { expression: "typeof HC === 'object'", returnByValue: true }, sessionId);
      if (r.result && r.result.result && r.result.result.value === true) {
        return;
      }
    }
    throw new Error("the game did not load");
  };
  await ev.loaded();
  return ev;
}
function check(name, ok, got) {
  console.log((ok ? "OK   " : "FAIL ") + name + (ok ? "" : " → " + JSON.stringify(got)));
  if (!ok) {
    process.exitCode = 1;
  }
}
async function until(ev, expr, ms = 15000) {
  const t = Date.now();
  let v;
  while (Date.now() - t < ms) {
    v = await ev(expr);
    if (v) {
      return v;
    }
    await sleep(100);
  }
  return v;
}
const peer = "[...HC.peers.values()][0]";

// All tabs share one localStorage, so the order matters: a refused player clears "stay online" for everyone.
const a = await tab();
check("menu offers the network game on the game server", await until(a, `!document.querySelector("#netAct").hidden`));
const seedA = await a(`HC.seed`);
await a(`HC.newWorld(HC.seed); HC.setTile(600, 40, HC.B.PINK); HC.netJoin("Аня", ${JSON.stringify(pass)}); 1`);
check("A is online", await until(a, `HC.net.ready`));

const b = await tab();
await b(`HC.netJoin("Боря", ${JSON.stringify(pass)}); 1`);
check("B is online", await until(b, `HC.net.ready`));
// Over a slow link B may come before A's world reaches the server: B then offers its own and gets A's back.
check("B got A's seed", await until(b, `HC.seed === ${seedA}`), await b(`HC.seed`));
check("B got A's block placed before sharing", await until(b, `HC.get(600, 40) === HC.B.PINK`));
check("A sees B", await until(a, `HC.peers.size === 1 && ${peer}.name === "Боря"`));
check("B sees A where A stands", await until(b, `${peer} && ${peer}.seen && Math.abs(${peer}.tx - HC.P.x) < 100`));

const cell = await a(`(() => {
  const x = Math.floor(HC.P.x) - 4;
  let y = 0;
  while ([HC.B.AIR, HC.B.LEAVES, HC.B.LOG].includes(HC.get(x, y))) y++;
  HC.finishBreak(x, y);
  return [x, y, HC.get(x, y)];
})()`);
check("B sees the block A broke", await until(b, `HC.get(${cell[0]}, ${cell[1]}) === ${cell[2]}`), cell);
const placed = await b(`(() => {
  HC.addItem(HC.B.GLASS, 5);
  const s = HC.inv.findIndex(s => s && s.id === HC.B.GLASS);
  [HC.inv[0], HC.inv[s]] = [HC.inv[s], HC.inv[0]];
  for (const [dx, dy] of [[2, -2], [-2, -2], [1, -3], [-1, -3], [0, -3], [2, -1], [-2, -1], [3, -1], [-3, -1]]) {
    const x = Math.floor(HC.P.x) + dx, y = Math.floor(HC.P.y) + dy;
    if (HC.get(x, y) === HC.B.AIR) {
      HC.usePlace(x, y);
      if (HC.get(x, y) === HC.B.GLASS) return [x, y];
    }
  }
  return null;
})()`);
check("B placed glass", placed !== null);
check("A sees B's glass", await until(a, `HC.get(${placed[0]}, ${placed[1]}) === HC.B.GLASS`), placed);

const chest = await a(`(() => {
  const x = Math.floor(HC.P.x) + 6;
  let y = 0;
  while ([HC.B.AIR, HC.B.LEAVES, HC.B.LOG].includes(HC.get(x, y))) y++;
  HC.setTile(x, y, HC.B.CHEST);
  HC.chestAt(x, y)[0] = { id: HC.B.GOLD_BLOCK, n: 7 };
  return [x, y];
})()`);
await b(`HC.setTile(${chest[0]}, ${chest[1]}, HC.B.CHEST); HC.finishBreak(${chest[0]}, ${chest[1]}); 1`);
check("A's things come to the bag when B breaks A's chest", await until(a, `HC.count(HC.B.GOLD_BLOCK) === 7 && !HC.chests.size`),
  await a(`HC.count(HC.B.GOLD_BLOCK)`));

const bx0 = await b(`HC.P.x`);
for (const key of ["KeyD", "KeyA"]) {
  await b(`HC.keys.add("${key}"); 1`);
  await sleep(key === "KeyD" ? 700 : 1400);
  await b(`HC.keys.clear(); 1`);
  if (Math.abs(await b(`HC.P.x`) - bx0) > 0.5) {
    break;
  }
}
await sleep(800);
let seen = false, bx;
for (let i = 0; i < 20 && !seen; i++) {
  bx = await b(`Math.round(HC.P.x * 100) / 100`);
  seen = await a(`Math.abs(${peer}.tx - ${bx}) < 0.02`);
  await sleep(200);
}
check("A sees B walk", seen && Math.abs(bx - bx0) > 0.5, [bx0, bx]);
if (shotFile) {
  await a.shot(shotFile);
}

await b(`location.reload(); 1`);
await sleep(500);
await b.loaded();
check("B is back in the same world after a reload", await until(b, `HC.net.ready && HC.get(${placed[0]}, ${placed[1]}) === HC.B.GLASS`));
check("A sees B again", await until(a, `HC.peers.size === 1`));

const c = await tab();
await c(`HC.netJoin("Вера", "bad"); 1`);
check("wrong password is refused", await until(c, `!HC.net.want && /Неверный пароль/.test(document.querySelector("#netStatus").textContent)`),
  await c(`document.querySelector("#netStatus").textContent`));

await b(`HC.newWorld(5); HC.netLeave(""); 1`);
check("A sees B leave", await until(a, `HC.peers.size === 0`));
check("A's counter shows one player", await until(a, `document.querySelector("#netHud").textContent === "🌐 В сети: 1"`),
  await a(`document.querySelector("#netHud").textContent`));
check("no page errors", pageErrors.length === 0, pageErrors);
process.exit();
