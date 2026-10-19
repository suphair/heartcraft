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
async function player(url = base) {
  const { result: { browserContextId } } = await call("Target.createBrowserContext");
  const { result: { targetId } } = await call("Target.createTarget", { url: "about:blank", browserContextId });
  const { result: { sessionId } } = await call("Target.attachToTarget", { targetId, flatten: true });
  await call("Runtime.enable", {}, sessionId);
  await call("Page.enable", {}, sessionId);
  await call("Page.navigate", { url }, sessionId);
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
check("the login screen links the single player game", await a(`document.querySelector("#soloLink").href`) === "https://suphair.github.io/heartcraft/");
await a(`document.querySelector("#login").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  window.dispatchEvent(new KeyboardEvent("keydown", { code: "Escape" })); 1`);
check("the login screen does not close by itself", await a(visible("login")));
check("the network game shows no saves and no test mode", await a(`[...document.querySelectorAll('[data-act="save"], [data-act="load"], #testBtn, #giveBtn')].every(el => getComputedStyle(el).display === "none")`));
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

const hold = (ev, id) => ev(`(() => {
  const s = HC.inv.findIndex(s => s && s.id === ${id});
  [HC.inv[HC.sel], HC.inv[s]] = [HC.inv[s], HC.inv[HC.sel]];
  return 1;
})()`);
const craftOut = (ev, id) => ev(`HC.craft(HC.RECIPES.findIndex(r => r.out === ${id}), 1)`);
await a(`HC.addItem(HC.I.IRON, 5); HC.addItem(HC.I.STICK, 1); 1`);
check("a sword is made of a stick and two iron ingots", await craftOut(a, "HC.I.SWORD") === 1 && await a(`HC.count(HC.I.IRON) === 3 && HC.count(HC.I.STICK) === 0`));
check("a bucket is made of three iron ingots", await craftOut(a, "HC.I.BUCKET") === 1 && await a(`HC.count(HC.I.IRON) === 0`));
await a(`HC.addItem(HC.I.GOLD, 6); HC.addItem(HC.I.COAL, 6); HC.addItem(HC.I.PINK_GEM, 3); HC.addItem(HC.B.SEAWEED, 3); 1`);
check("gold ore and coal make a gold ingot", await a(`HC.craft(HC.RECIPES.findIndex(r => r.out === HC.I.GOLD_INGOT), 10)`) === 6);
check("three gold ingots and three pink gems make a heart egg", await craftOut(a, "HC.I.HEART_EGG") === 1 && await a(`HC.count(HC.I.PINK_GEM) === 0`));
check("three gold ingots and three seaweed make a fish egg", await craftOut(a, "HC.I.FISH_EGG") === 1 && await a(`HC.count(HC.I.GOLD_INGOT) === 0 && HC.count(HC.B.SEAWEED) === 0`));
const weed = await a(`(() => {
  const x = Math.floor(HC.P.x) + 6, top = Math.floor(HC.P.y) - 8;
  for (let y = top; y < top + 7; y++) HC.setTile(x, y, y >= top + 3 ? HC.B.SEAWEED : HC.B.WATER);
  const first = HC.growSeaweed(1);
  const grown = HC.growSeaweed(600) && HC.get(x, top + 2) === HC.B.SEAWEED;
  HC.growSeaweed(600);
  return [first, grown, HC.get(x, top + 1) === HC.B.WATER, x, top + 2];
})()`);
check("the first player grows seaweed once in ten minutes", !weed[0] && weed[1], weed);
check("seaweed stops at five blocks", weed[2], weed);
check("the others see the seaweed grow", await until(b, `HC.get(${weed[3]}, ${weed[4]}) === HC.B.SEAWEED`));
check("only the first player grows seaweed", await b(`HC.growSeaweed(600)`) === false);
check("hearts do not age", await a(`HC.mobs.length > 0 && HC.mobs.every(m => !("age" in m) && !("life" in m))`));
await hold(a, "HC.I.SWORD");
const kill = await a(`(() => {
  const before = HC.mobs.length, m = HC.mobs[0];
  m.x = HC.P.x + 1;
  m.y = HC.P.y;
  const r = document.querySelector("#view").getBoundingClientRect();
  document.querySelector("#view").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerType: "mouse",
    clientX: r.left + (m.x + m.w / 2 - HC.cam.x) * HC.TILE, clientY: r.top + (m.y + m.h / 2 - HC.cam.y) * HC.TILE }));
  return [before - HC.mobs.length, HC.inv[HC.sel].dur, HC.mobs.includes(m)];
})()`);
check("the sword kills a heart and wears out", kill[0] === 1 && kill[1] === 149 && !kill[2], kill);
await hold(a, "HC.I.BUCKET");
const water = await a(`(() => {
  const cells = [];
  for (const [dx, dy] of [[2, -2], [-2, -2], [1, -3], [-1, -3], [0, -3], [2, -1], [-2, -1], [3, -1], [-3, -1]]) {
    const x = Math.floor(HC.P.x) + dx, y = Math.floor(HC.P.y) + dy;
    if (HC.get(x, y) === HC.B.AIR && HC.get(x - 1, y) !== HC.B.WATER && HC.get(x + 1, y) !== HC.B.WATER) cells.push([x, y]);
  }
  const [from, to] = cells;
  HC.setTile(from[0], from[1], HC.B.WATER);
  HC.usePlace(from[0], from[1]);
  const scooped = HC.get(from[0], from[1]) === HC.B.AIR && HC.inv[HC.sel].id === HC.I.WATER_BUCKET;
  HC.usePlace(to[0], to[1]);
  return [scooped, HC.get(to[0], to[1]) === HC.B.WATER && HC.inv[HC.sel].id === HC.I.BUCKET, to];
})()`);
check("a bucket scoops water up", water[0], water);
check("a bucket pours the water out", water[1], water);
check("B sees the poured water", await until(b, `HC.get(${water[2][0]}, ${water[2][1]}) === HC.B.WATER`));
await a(`(() => {
  document.querySelector("#shareBtn").click();
  [...document.querySelectorAll("#sharePlayers button")].find(b => /Боря/.test(b.textContent)).click();
  const slot = HC.inv.findIndex(s => s && s.id === HC.I.SWORD);
  document.querySelectorAll("#pass .grid .slot")[slot].dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  return 1;
})()`);
check("A passes the sword to B", await until(b, `HC.inv.some(s => s && s.id === HC.I.SWORD && s.dur === 149)`) && await a(`HC.count(HC.I.SWORD) === 0`),
  await b(`HC.inv.filter(Boolean)`));
await a(`document.querySelector("#pass .close").click(); 1`);
const passTo = (ev, name, id) => ev(`(() => {
  document.querySelector("#shareBtn").click();
  [...document.querySelectorAll("#sharePlayers button")].find(b => b.textContent.endsWith(${JSON.stringify(name)})).click();
  const slot = HC.inv.findIndex(s => s && s.id === ${id});
  document.querySelectorAll("#pass .grid .slot")[slot].dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  document.querySelector("#pass .close").click();
  return 1;
})()`);
const bagB = await b(`JSON.stringify(HC.inv)`);
await b(`HC.inv.forEach((s, i) => (HC.inv[i] = { id: HC.B.DIRT, n: 99 })); 1`);
await a(`HC.addItem(HC.B.COBBLE, 5); 1`);
await passTo(a, "Боря", "HC.B.COBBLE");
check("what does not fit into a full bag comes back", await until(a, `HC.count(HC.B.COBBLE) === 5 && /рюкзак полон — вернулось/.test(document.querySelector("#toast").textContent)`) && await b(`HC.count(HC.B.COBBLE) === 0`),
  await a(`HC.count(HC.B.COBBLE)`));
await b(`(() => {
  const saved = JSON.parse(${JSON.stringify(bagB)});
  saved.forEach((s, i) => (HC.inv[i] = s));
  return 1;
})()`);
await b(`(() => {
  document.querySelector("#menuBtn").click();
  document.querySelector('[data-act="look"]').click();
  return 1;
})()`);
check("the menu opens the look with the current one chosen", await until(b, `${visible("hero")} && document.querySelectorAll("#lookHats button")[4].classList.contains("on")`));
await chooseHero(b, 0, 5);
check("A sees B in the pink crown", await until(a, `${peer("Боря")}.look.h === 5`));
check("B keeps the new look", await until(b, `document.querySelector("#hero").hidden && HC.net.look.h === 5`));

check("A, who came first, runs the mobs", await a(`HC.leading()`) && !(await b(`HC.leading()`)));
check("there are sheep in the world", await a(`HC.sheep.length > 0`));
const sameMobs = `JSON.stringify(HC.mobs.map(m => m.id).sort()) === ${JSON.stringify(JSON.stringify(await a(`HC.mobs.map(m => m.id).sort()`)))}`;
check("B sees the same hearts as A", await until(b, sameMobs), await b(`HC.mobs.map(m => m.id)`));
const drift = await b(`(() => {
  const theirs = ${JSON.stringify(await a(`HC.mobs.map(m => [m.id, m.x, m.y])`))};
  return Math.max(...theirs.map(([id, x, y]) => { const m = HC.mobs.find(o => o.id === id); return Math.hypot(m.x - x, m.y - y); }));
})()`);
check("B sees the hearts where A has them", drift < 2, drift);
await hold(b, "HC.I.SWORD");
const lamb = await b(`(() => {
  const o = HC.sheep[0];
  o.x = o.tx = HC.P.x + 1;
  o.y = o.ty = HC.P.y;
  const r = document.querySelector("#view").getBoundingClientRect();
  document.querySelector("#view").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerType: "mouse",
    clientX: r.left + (o.x + o.w / 2 - HC.cam.x) * HC.TILE, clientY: r.top + (o.y + o.h / 2 - HC.cam.y) * HC.TILE }));
  return [o.id, HC.count(HC.B.WOOL)];
})()`);
check("a sheep killed with the sword gives wool", lamb[1] === 1, lamb);
check("the sheep B killed is gone for A too", await until(a, `!HC.sheep.some(o => o.id === ${lamb[0]})`));
await b(`HC.addItem(HC.B.PINK, 1); 1`);
await hold(b, "HC.B.PINK");
const loved = await b(`(() => {
  const m = HC.mobs.find(o => o.grow <= 0 && o.love <= 0 && o.pause <= 0);
  m.x = m.tx = HC.P.x + 1;
  m.y = m.ty = HC.P.y;
  HC.feed(m);
  return m.id;
})()`);
check("a heart B makes fall in love is in love for A", await until(a, `HC.mobs.some(m => m.id === ${loved} && m.love > 0)`));
await a(`HC.addItem(HC.B.PLANKS, 3); HC.addItem(HC.B.WOOL, 3); 1`);
check("a bed is made of three planks and three wool", await craftOut(a, "HC.B.BED") === 1);
const bedAt = await place(a, "HC.B.BED");
check("A placed a bed", bedAt !== null);
check("there is no bed button before a bed is chosen", await a(`document.querySelector("#bedBtn").hidden`));
await a(`HC.usePlace(${bedAt[0]}, ${bedAt[1]}); 1`);
check("pressing the bed makes it A's", await a(`JSON.stringify(HC.bed)`) === JSON.stringify({ x: bedAt[0], y: bedAt[1] }));
check("the bed button shows up", await until(a, `!document.querySelector("#bedBtn").hidden`));
await a(`HC.P.x += 12; document.querySelector("#bedBtn").click(); 1`);
check("the bed button brings A to the bed", await a(`Math.floor(HC.P.x + HC.P.w / 2) === ${bedAt[0]} && Math.floor(HC.P.y + HC.P.h / 2) === ${bedAt[1]}`),
  await a(`[HC.P.x, HC.P.y]`));
await a(`(() => {
  document.querySelector("#menuBtn").click();
  document.querySelector('[data-act="settings"]').click();
  document.querySelector("#dayBtn").click();
  return 1;
})()`);
check("the settings hold the sound and the day", await a(`${visible("settings")} && /Звук/.test(document.querySelector("#soundBtn").textContent) && /Всегда день: да/.test(document.querySelector("#dayBtn").textContent)`));
check("the sound and day buttons left the top bar", await a(`!document.querySelector("#top #soundBtn") && !document.querySelector("#top #dayBtn")`));
await a(`document.querySelector("#dayBtn").click(); document.querySelector("#settings .close").click(); 1`);

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
check("A keeps the bed after the reload", await a(`JSON.stringify(HC.bed)`) === JSON.stringify({ x: bedAt[0], y: bedAt[1] }));

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
})()`) === "🟢 Аня🎁🚫,🟢 Боря🎁🚫|2", await boss(`document.querySelectorAll("#adminWorlds .world")[0].textContent`));
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
await boss(`window.prompt = () => "грубил"; [...document.querySelectorAll("#adminWorlds .heroCard")].find(c => /Боря/.test(c.textContent)).querySelectorAll(".btn")[1].click(); 1`);
check("a banned player is sent out with the reason", await until(e, `${visible("login")} && /забанен: грубил/.test(document.querySelector("#loginStatus").textContent)`),
  await e(`document.querySelector("#loginStatus").textContent`));
check("the admin sees the ban with the reason", await until(boss, `/Боря — грубил/.test(document.querySelector("#bans").textContent)`));
await e.login("БОРЯ", MINT);
check("a banned login is refused in every world", await until(e, `/забанен/.test(document.querySelector("#loginStatus").textContent) && !HC.net.ready`));
await boss(`[...document.querySelectorAll("#bans button")].find(b => /Разбанить/.test(b.textContent)).click(); 1`);
check("the admin lifts the ban", await until(boss, `/никого/.test(document.querySelector("#bans").textContent)`));
await e.login("боря", ROSE);
check("an unbanned player plays again", await until(e, `HC.net.ready && document.querySelector("#login").hidden`));
check("notes are hidden behind their tab", await boss(`${visible("tab-worlds")} && !${visible("tab-notes")}`));
await boss(`document.querySelector('#adminTabs [data-tab="notes"]').click(); 1`);
check("the notes tab shows the notes instead of the worlds", await boss(`${visible("tab-notes")} && !${visible("tab-worlds")}`));
await boss(`(() => {
  document.querySelector("#noteText").value = "Добавить лук";
  document.querySelector("#noteAdd").click();
  return 1;
})()`);
check("the admin adds a note", await until(boss, `/🆕 №1 · .*: Добавить лук/.test(document.querySelector("#notes").textContent)`),
  await boss(`document.querySelector("#notes").textContent`));
await boss(`window.prompt = () => "Добавить лук и стрелы"; [...document.querySelectorAll("#notes button")][0].click(); 1`);
check("the admin edits a note", await until(boss, `/№1 · .*: Добавить лук и стрелы/.test(document.querySelector("#notes").textContent)`));
await boss(`window.confirm = () => true; document.querySelector('#notes button[title="Удалить"]').click(); 1`);
check("the admin deletes a note", await until(boss, `/пока нет/.test(document.querySelector("#notes").textContent)`));
await boss(`(() => {
  document.querySelector("#noteText").value = "Ещё одно";
  document.querySelector("#noteAdd").click();
  return 1;
})()`);
await until(boss, `/Ещё одно/.test(document.querySelector("#notes").textContent)`);
check("the notes tab counts the new notes", await boss(`document.querySelector('#adminTabs [data-tab="notes"]').textContent`) === "📝 Замечания (1 🆕)",
  await boss(`document.querySelector('#adminTabs [data-tab="notes"]').textContent`));
await boss(`document.querySelector("#adminExit").click(); 1`);
check("the admin logs out to the login screen", await until(boss, `${visible("login")} && document.querySelector("#admin").hidden`));

await a(`document.querySelector("#menuBtn").click(); 1`);
check("the menu lists the players", await a(`/Боря/.test(document.querySelector("#players").textContent)`));
await a(`document.querySelector('[data-act="logout"]').click(); 1`);
check("logout returns to the login screen", await until(a, `${visible("login")} && !HC.net.ready`));
check("the others see A leave", await until(e, `!${peer("Аня")}`));
const solo = await player(base + "?solo");
const shown = id => `getComputedStyle(document.querySelector("#${id}")).display !== "none" && !document.querySelector("#${id}").hidden`;
check("the single player game starts without a login", await until(solo, `HC.SOLO && !HC.net.ready && document.querySelector("#login").hidden && HC.count(HC.B.WORKBENCH) === 1`));
check("the single player game has no network buttons", await solo(`!(${shown("shareBtn")}) && ${shown("testBtn")} && !(${shown("players")})`));
await solo(`document.querySelector("#testBtn").click(); document.querySelector("#giveBtn").click(); 1`);
await solo(`[...document.querySelectorAll("#give .grid .slot")].find(el => el.title.startsWith("Меч")).dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })); 1`);
check("test mode gives things", await solo(`HC.testMode && HC.count(HC.I.SWORD) === 1`));
check("the single player gives with a box, not a gift", await solo(`document.querySelector("#giveBtn").textContent`) === "📦");
await solo(`document.querySelector("#give .close").click(); 1`);
check("seaweed grows in the single player game", await solo(`HC.growSeaweed(600)`) === true);
const flock = await solo(`HC.sheep.length`);
check("the single player game has sheep", flock > 0);
await solo(`HC.addItem(HC.B.BRICK, 7); document.querySelector("#menuBtn").click(); document.querySelector('[data-act="save"]').click(); 1`);
const soloSeed = await solo(`HC.seed`);
await solo(`location.reload(); 1`);
await sleep(500);
await solo.loaded();
check("a saved single player world comes back after a reload", await until(solo, `HC.seed === ${soloSeed} && HC.count(HC.B.BRICK) === 7 && HC.count(HC.I.SWORD) === 1 && HC.sheep.length === ${flock}`),
  await solo(`[HC.seed, HC.count(HC.B.BRICK)]`));
await solo(`(() => {
  document.querySelector("#menuBtn").click();
  document.querySelector('[data-act="reset"]').click();
  document.querySelector("#soloSeed").value = 4242;
  const s = { hills: 4, ocean: 0, caves: 3, ores: 2, trees: 3, hearts: 0 };
  for (const key in s) document.querySelector("#solo-" + key).value = s[key];
  document.querySelector("#soloCreate").click();
  return 1;
})()`);
check("a new single player world follows its settings", await solo(`HC.seed === 4242 && JSON.stringify(HC.worldOpts) === ${JSON.stringify(JSON.stringify({ hills: 4, ocean: 0, caves: 3, ores: 2, trees: 3, hearts: 0 }))} && HC.mobs.length === 0`),
  await solo(`[HC.seed, JSON.stringify(HC.worldOpts)]`));
const old = await solo(`(() => {
  const d = JSON.parse(HC.serialize());
  d.v = 4;
  delete d.opts;
  d.ocean = true;
  d.mobs = [[d.player.x, d.player.y, 0, 1, 100, 900]];
  HC.loadText(JSON.stringify(d), "ok");
  return [document.querySelector("#toast").textContent, JSON.stringify(HC.worldOpts), HC.mobs.length, HC.mobs[0].white];
})()`);
check("a save of version 1.3 still opens", old[0] === "ok" && old[1] === JSON.stringify({ hills: 2, ocean: 1, caves: 2, ores: 1, trees: 2, hearts: 2 }) && old[2] === 1 && old[3] === true, old);
await solo(`(() => {
  document.querySelector("#menuBtn").click();
  document.querySelector('[data-act="look"]').click();
  return 1;
})()`);
await chooseHero(solo, 6, 5);
await solo(`location.reload(); 1`);
await sleep(500);
await solo.loaded();
check("the single player keeps the look", await until(solo, `JSON.stringify(HC.net.look) === '{"c":6,"h":5}'`), await solo(`JSON.stringify(HC.net.look)`));
await solo.shot("solo");
check("no page errors", pageErrors.length === 0, pageErrors);
process.exit();
