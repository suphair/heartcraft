// Players in headless Chrome tabs log in to a local server and play in its worlds.
// Run through tests/e2e.sh, which starts the server with worlds "rose" and "mint" and the browser.
//
//   node tests/e2e.mjs <devtools port> <game url> [screenshot dir]
// The world codes are "rose" and "mint" and the admin is "boss" with code 999 (tests/e2e.sh).
const [, , port, base, shotDir] = process.argv;
const ROSE = "rose", MINT = "mint";
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

// Every player gets a browser context of its own: tabs of one context share localStorage and the remembered login.
async function player() {
  const { result: { browserContextId } } = await call("Target.createBrowserContext");
  const { result: { targetId } } = await call("Target.createTarget", { url: "about:blank", browserContextId });
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
  ev.loaded = async () => {
    for (let t = Date.now(); Date.now() - t < 20000; await sleep(200)) {
      const r = await call("Runtime.evaluate", { expression: "typeof HC === 'object'", returnByValue: true }, sessionId);
      if (r.result && r.result.result && r.result.result.value === true) {
        return;
      }
    }
    throw new Error("the game did not load");
  };
  ev.shot = async name => {
    if (!shotDir) {
      return;
    }
    await call("Page.bringToFront", {}, sessionId);
    await sleep(800);
    const r = await call("Page.captureScreenshot", { format: "png" }, sessionId);
    (await import("node:fs")).writeFileSync(`${shotDir}/${name}.png`, Buffer.from(r.result.data, "base64"));
  };
  ev.login = (login, code) => ev(`(() => {
    document.querySelector("#loginName").value = ${JSON.stringify(login)};
    document.querySelector("#loginCode").value = ${JSON.stringify(code)};
    document.querySelector("#loginBtn").click();
    return 1;
  })()`);
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
const visible = id => `!document.querySelector("#${id}").hidden`;
const peer = name => `[...HC.peers.values()].find(o => o.name === ${JSON.stringify(name)})`;
const chooseHero = (ev, c, h) => ev(`(() => {
  document.querySelectorAll("#lookColors button")[${c}].click();
  document.querySelectorAll("#lookHats button")[${h}].click();
  document.querySelector("#heroBtn").click();
  return 1;
})()`);

const a = await player();
check("the game starts at the login screen", await a(visible("login")));
await a(`document.querySelector("#login").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  window.dispatchEvent(new KeyboardEvent("keydown", { code: "Escape" })); 1`);
check("the login screen does not close by itself", await a(visible("login")));
check("there are no saves and no test mode", await a(`!document.querySelector('[data-act="save"], [data-act="load"], #testBtn, #giveBtn, #file')`));
await a.login("Аня", ROSE);
check("a new hero chooses a look", await until(a, visible("hero")));
await a.shot("hero");
await chooseHero(a, 5, 2);
check("A is in the game", await until(a, `HC.net.ready && document.querySelector("#login").hidden && document.querySelector("#hero").hidden`));
check("A keeps the chosen look", await a(`JSON.stringify(HC.net.look)`) === '{"c":5,"h":2}', await a(`JSON.stringify(HC.net.look)`));
check("A starts with a workbench", await a(`HC.count(HC.B.WORKBENCH) === 1`));
const seedA = await a(`HC.seed`);

const b = await player();
await b.login("Боря", ROSE);
await until(b, visible("hero"));
await chooseHero(b, 0, 4);
check("B is in the game", await until(b, `HC.net.ready && document.querySelector("#hero").hidden`));
check("B is in A's world", await until(b, `HC.seed === ${seedA}`), await b(`HC.seed`));
check("A sees B with B's look", await until(a, `${peer("Боря")} && ${peer("Боря")}.look.h === 4`));
check("B sees A with A's look", await until(b, `${peer("Аня")} && ${peer("Аня")}.look.c === 5 && ${peer("Аня")}.seen`));

const cell = await a(`(() => {
  const x = Math.floor(HC.P.x) - 4;
  let y = 0;
  while ([HC.B.AIR, HC.B.LEAVES, HC.B.LOG].includes(HC.get(x, y))) y++;
  HC.finishBreak(x, y);
  return [x, y, HC.get(x, y)];
})()`);
check("B sees the block A broke", await until(b, `HC.get(${cell[0]}, ${cell[1]}) === ${cell[2]}`), cell);
const place = (ev, id) => ev(`(() => {
  HC.addItem(${id}, 1);
  const s = HC.inv.findIndex(s => s && s.id === ${id});
  [HC.inv[HC.sel], HC.inv[s]] = [HC.inv[s], HC.inv[HC.sel]];
  for (const [dx, dy] of [[2, -2], [-2, -2], [1, -3], [-1, -3], [0, -3], [2, -1], [-2, -1], [3, -1], [-3, -1]]) {
    const x = Math.floor(HC.P.x) + dx, y = Math.floor(HC.P.y) + dy;
    if (HC.get(x, y) === HC.B.AIR) {
      HC.usePlace(x, y);
      if (HC.get(x, y) === ${id}) return [x, y];
    }
  }
  return null;
})()`);
const glass = await place(b, "HC.B.GLASS");
check("B placed glass", glass !== null);
check("A sees B's glass", await until(a, `HC.get(${glass[0]}, ${glass[1]}) === HC.B.GLASS`), glass);

const chest = await place(a, "HC.B.CHEST");
check("A placed a chest", chest !== null);
await a(`HC.openChest(${chest[0]}, ${chest[1]}); HC.chestAt(${chest[0]}, ${chest[1]})[0] = { id: HC.B.GOLD_BLOCK, n: 7 }; 1`);
const key = chest[1] * 1024 + chest[0];
check("B sees what A put into the chest", await until(b, `HC.chests.get(${key}) && HC.chests.get(${key})[0] && HC.chests.get(${key})[0].n === 7`));
check("B cannot break a chest with things in it", await b(`HC.finishBreak(${chest[0]}, ${chest[1]}) === false`));
await a(`document.querySelector("#store .close").click(); 1`);
check("the egg recipes are at the workbench", await a(`HC.RECIPES.some(r => r.out === HC.I.HEART_EGG) && HC.RECIPES.some(r => r.out === HC.I.FISH_EGG)`));

await a(`HC.addItem(HC.B.BRICK, 5); 1`);
const ax = await a(`HC.P.x`);
await sleep(500);
await a.shot("two");
await a(`location.reload(); 1`);
await sleep(500);
await a.loaded();
check("after a reload A is back without the login screen", await until(a, `HC.net.ready && document.querySelector("#login").hidden`));
check("A keeps the bag", await a(`HC.count(HC.B.BRICK) === 5 && HC.count(HC.B.WORKBENCH) === 1`), await a(`HC.inv.filter(Boolean)`));
check("A is where A stood", Math.abs(await a(`HC.P.x`) - ax) < 1, [ax, await a(`HC.P.x`)]);
check("A is not asked for a look again", await a(`document.querySelector("#hero").hidden`));
check("A sees B's glass after the reload", await a(`HC.get(${glass[0]}, ${glass[1]}) === HC.B.GLASS`));
check("A sees the chest contents after the reload", await a(`HC.chests.get(${key})[0].n === 7`));

const c = await player();
await c.login("Аня", MINT);
check("the same login in another world is a new hero", await until(c, visible("hero")));
await chooseHero(c, 1, 1);
check("the other world is another world", await until(c, `HC.net.ready && HC.peers.size === 0`) && (await c(`HC.seed`)) !== seedA);
check("A stays in the first world", await a(`HC.net.ready`));

const d = await player();
await d.login("Вера", "nope");
check("a wrong world code is refused", await until(d, `/Неверный код мира/.test(document.querySelector("#loginStatus").textContent)`),
  await d(`document.querySelector("#loginStatus").textContent`));
check("the login form is open again", await d(`!document.querySelector("#loginBtn").disabled && ${visible("login")}`));

const e = await player();
await e.login("боря", ROSE);
check("B's login on another device takes B's hero", await until(e, `HC.net.ready && document.querySelector("#hero").hidden`));
check("the first device of B is sent to the login screen", await until(b, `${visible("login")} && /другого устройства/.test(document.querySelector("#loginStatus").textContent)`),
  await b(`document.querySelector("#loginStatus").textContent`));

const boss = await player();
await boss.login("Boss", "999");
check("the admin gets the admin panel", await until(boss, visible("admin")));
check("the admin sees the worlds with codes", await boss(`[...document.querySelectorAll("#adminWorlds code")].map(c => c.textContent).join()`) === "rose,mint");
check("the admin sees the heroes with pictures", await boss(`(() => {
  const rose = document.querySelectorAll("#adminWorlds .world")[0];
  return [...rose.querySelectorAll(".heroCard")].map(c => c.textContent).join() + "|" + rose.querySelectorAll(".heroCard canvas").length;
})()`) === "🟢 Аня🎁,🟢 Боря🎁|2", await boss(`document.querySelectorAll("#adminWorlds .world")[0].textContent`));
const seeds = new Set();
for (let i = 0; i < 4; i++) {
  await boss(`document.querySelector("#newRandom").click(); 1`);
  seeds.add(await boss(`JSON.stringify([document.querySelector("#newSeed").value, ...[...document.querySelectorAll("#newOpts select")].map(s => s.value)])`));
}
check("the dice shuffle the settings", seeds.size === 4, [...seeds]);
const settings = { seed: 4242, hills: 4, ocean: 0, caves: 3, ores: 2, trees: 3, hearts: 0 };
await boss(`(() => {
  document.querySelector("#newName").value = "Горы";
  document.querySelector("#newCode").value = "peaks";
  document.querySelector("#newSeed").value = 4242;
  const s = ${JSON.stringify(settings)};
  for (const key of ["hills", "ocean", "caves", "ores", "trees", "hearts"]) document.querySelector("#new-" + key).value = s[key];
  document.querySelector("#newCreate").click();
  return 1;
})()`);
check("the admin creates a world", await until(boss, `[...document.querySelectorAll("#adminWorlds code")].some(c => c.textContent === "peaks")`));
await boss.shot("admin");
const f = await player();
await f.login("Гоша", "peaks");
await until(f, visible("hero"));
await chooseHero(f, 2, 3);
check("the new world follows its settings", await until(f, `HC.net.ready && HC.seed === 4242 && JSON.stringify(HC.worldOpts) === ${JSON.stringify(JSON.stringify({ hills: 4, ocean: 0, caves: 3, ores: 2, trees: 3, hearts: 0 }))}`),
  await f(`[HC.seed, JSON.stringify(HC.worldOpts)]`));
check("a world without ocean has no water and no hearts", await f(`!HC.world.includes(HC.B.WATER) && HC.mobs.length === 0`));
await f.shot("peaks");
check("the admin panel shows a hero who has just come", await until(boss, `[...document.querySelectorAll("#adminWorlds .heroCard")].some(c => /Гоша/.test(c.textContent))`));
await boss(`(() => {
  const card = [...document.querySelectorAll("#adminWorlds .heroCard")].find(c => /Гоша/.test(c.textContent));
  card.querySelector(".btn").click();
  document.querySelector("#giftN").value = 12;
  const slot = [...document.querySelectorAll("#gift .grid .slot")].find(el => el.title === "Золотой блок");
  slot.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  return 1;
})()`);
check("a gift reaches a hero in the game", await until(f, `HC.count(HC.B.GOLD_BLOCK) === 12`));
await boss(`document.querySelector("#giftBack").click(); 1`);
await boss(`window.confirm = () => true; [...document.querySelectorAll("#adminWorlds .world")].find(w => /peaks/.test(w.textContent)).querySelector(".btn").click(); 1`);
check("deleting a world sends its players out", await until(f, `${visible("login")} && /удалён/.test(document.querySelector("#loginStatus").textContent)`),
  await f(`document.querySelector("#loginStatus").textContent`));
check("the deleted world is gone from the list", await until(boss, `![...document.querySelectorAll("#adminWorlds code")].some(c => c.textContent === "peaks")`));
await boss(`document.querySelector("#adminExit").click(); 1`);
check("the admin logs out to the login screen", await until(boss, `${visible("login")} && document.querySelector("#admin").hidden`));

await a(`document.querySelector("#menuBtn").click(); 1`);
check("the menu lists the players", await a(`/Боря/.test(document.querySelector("#players").textContent)`));
await a(`document.querySelector('[data-act="logout"]').click(); 1`);
check("logout returns to the login screen", await until(a, `${visible("login")} && !HC.net.ready`));
check("the others see A leave", await until(e, `!${peer("Аня")}`));
check("no page errors", pageErrors.length === 0, pageErrors);
process.exit();
