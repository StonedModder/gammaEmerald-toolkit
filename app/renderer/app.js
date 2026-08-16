'use strict';
const $ = (id) => document.getElementById(id);
const rpc = (m, p) => window.gamma.call(m, p);

let attached = false;
let hunting = false;
let statusTimer = null;

/* ------------------------------------------------------------------ chrome */
document.querySelectorAll('nav button').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('nav button').forEach((t) => {
      const on = t === tab;
      t.setAttribute('aria-selected', String(on));
      $('pane-' + t.dataset.pane).hidden = !on;
    });
    if (tab.dataset.pane === 'cheats') loadPokeRoster().catch(showError);
    // Save backups are plain file work, so this tab does not need an attach.
    if (tab.dataset.pane === 'tools') {
      loadSaves().catch(showError);
      loadMoney().catch(showError);
    }
  };
});

function log(line, cls) {
  const box = $('log');
  const el = document.createElement('div');
  const t = new Date().toLocaleTimeString([], { hour12: false });
  el.innerHTML = `<span class="t">${t}</span> `;
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = line;
  el.appendChild(span);
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  while (box.children.length > 400) box.removeChild(box.firstChild);
}

function showError(e) {
  $('err').textContent = e ? String(e.message || e) : '';
  if (e) log(String(e.message || e), 'bad');
}

function setLink(on, text) {
  $('pip').dataset.on = String(on);
  $('linkText').textContent = text;
}

function enable(ids, on) {
  ids.forEach((id) => { $(id).disabled = !on; });
}

/* -------------------------------------------------------------- odds meter */
function paintOdds(rate, oddsText, found) {
  const card = $('oddsCard');
  const known = rate !== null && rate !== undefined;
  const forced = known && Number(rate) <= 0;
  card.dataset.forced = String(forced);
  card.dataset.found = String(!!found);
  $('oddsText').textContent = oddsText || '—';
  $('oddsSub').textContent = known ? 'wild encounter roll' : 'not attached';
  // Same log scale as the odds slider below. They used different ranges, so the
  // gauge marker and the slider thumb sat in visibly different places for the
  // same number.
  const denom = known ? Math.max(1, Number(rate) + 1) : 1;
  const pos = Math.min(1, Math.log(denom) / Math.log(ODDS_MAX));
  $('oddsMarker').style.left = (pos * 100).toFixed(2) + '%';
  $('oddsMarker').style.opacity = known ? '1' : '.25';
}

function fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

function paintStats(st) {
  if (!st) return;
  $('sAttempts').textContent = st.total_resets ?? st.attempts ?? 0;
  $('sElapsed').textContent = fmtElapsed(st.elapsed);
  $('sRate').textContent = Math.round(st.per_hour ?? 0);
  $('sCycle').textContent = st.avg_reset ? st.avg_reset.toFixed(0) : '—';
  const total = st.total_shinies ?? 0;
  $('sFound').textContent = total || '—';
  $('statFound').dataset.hot = String(total > 0);
  paintTiming(st);
  paintRoster(st);
}

/* Where a cycle actually goes. Worth showing: the soft reset is nearly free and
   almost all of it is the game's own save load, so this is the honest answer to
   "why isn't it faster". */
function paintTiming(st) {
  const p = st.phases;
  if (!p || !Object.keys(p).length) return;
  const bits = [];
  if (p.roll != null) bits.push(`roll ${p.roll}s`);
  if (p.reset != null) bits.push(`soft reset ${p.reset}s`);
  if (p.title != null) bits.push(`title ${p.title}s`);
  if (p.load != null) bits.push(`save load ${p.load}s`);
  $('resetHint').textContent = bits.join(' · ');
}

/* ------------------------------------------------------------------ attach */
async function loadBuilds() {
  const { versions } = await rpc('app.versions');
  const sel = $('buildSel');
  sel.innerHTML = '';
  versions.forEach((v) => {
    const o = document.createElement('option');
    o.value = v.id;
    o.textContent = `${v.name}${v.installed ? '' : ' — not found'}`;
    o.disabled = !v.installed;
    sel.appendChild(o);
  });
  const ea = versions.find((v) => v.id === 'ea' && v.installed);
  if (ea) sel.value = 'ea';
}

let STARTERS = [];

async function loadStarters() {
  const { starters } = await rpc('app.starters');
  STARTERS = starters;
  const sel = $('starterSel');
  sel.innerHTML = '';
  starters.forEach((s) => {
    const o = document.createElement('option');
    o.value = s.id; o.textContent = s.name;
    sel.appendChild(o);
  });
  sel.value = 'treecko';

  const box = $('roster');
  box.innerHTML = '';
  starters.forEach((s) => {
    const card = document.createElement('div');
    card.className = 'mon';
    card.dataset.id = s.id;
    card.setAttribute('aria-selected', String(s.id === sel.value));
    card.innerHTML =
      `<img src="assets/${s.id}.gif" alt="${s.name}">` +
      `<span class="nm">${s.name}</span>` +
      `<span class="sc" id="sc-${s.id}">0 resets</span>`;
    card.onclick = () => selectStarter(s.id);
    box.appendChild(card);
  });
}

function selectStarter(id) {
  $('starterSel').value = id;
  document.querySelectorAll('.mon').forEach((c) =>
    c.setAttribute('aria-selected', String(c.dataset.id === id)));
}

/** Swap a card to its shiny sprite once that species has been found. */
function paintRoster(st) {
  if (!st || !st.resets) return;
  STARTERS.forEach((s) => {
    const card = document.querySelector(`.mon[data-id="${s.id}"]`);
    if (!card) return;
    const resets = st.resets[s.id] || 0;
    const shinies = st.shinies[s.id] || 0;
    const on = (st.found_on && st.found_on[s.id]) || [];
    const sc = $(`sc-${s.id}`);
    sc.innerHTML = shinies
      ? `<b>${shinies}✨</b> in ${resets} · @${on.join(', @')}`
      : `${resets} reset${resets === 1 ? '' : 's'}`;
    const isShiny = shinies > 0;
    card.dataset.shiny = String(isShiny);
    const img = card.querySelector('img');
    const want = `assets/${s.id}${isShiny ? '_shiny' : ''}.gif`;
    if (!img.src.endsWith(want)) img.src = want;
    let badge = card.querySelector('.badge');
    if (isShiny && !badge) {
      badge = document.createElement('span');
      badge.className = 'badge';
      card.appendChild(badge);
    }
    if (badge) badge.textContent = 'x' + shinies;
  });
}

async function refreshProcesses() {
  showError(null);
  try {
    const { processes } = await rpc('app.processes');
    const sel = $('pidSel');
    sel.innerHTML = '';
    if (!processes.length) {
      const o = document.createElement('option');
      o.textContent = 'no game running';
      o.value = '';
      sel.appendChild(o);
      $('attachHint').textContent = 'Start Pokemon Gamma Emerald, then refresh.';
      $('btnAttach').disabled = true;
      return;
    }
    processes.forEach((p) => {
      const o = document.createElement('option');
      o.value = String(p.pid);
      o.textContent = `pid ${p.pid} — ${p.mem_mb} MB${p.likely_game ? '' : ' (launcher)'}`;
      sel.appendChild(o);
    });
    const game = processes.find((p) => p.likely_game) || processes[0];
    sel.value = String(game.pid);
    $('attachHint').textContent = 'Ready to attach.';
    $('btnAttach').disabled = false;
  } catch (e) { showError(e); }
}

function renderChecks(checks, pending) {
  const box = $('checks');
  box.hidden = false;
  box.textContent = '';
  (checks || []).forEach((c) => {
    const d = document.createElement('div');
    d.className = 'check';
    d.dataset.ok = String(c.ok);
    d.innerHTML = `<i>${c.ok ? '✓' : '✗'}</i><span>${c.name}</span><em>${c.detail}</em>`;
    box.appendChild(d);
  });
  if (pending) {
    const d = document.createElement('div');
    d.className = 'check';
    d.dataset.ok = 'pending';
    d.innerHTML = '<i>·</i><span>checking offsets</span><em>runs in the background</em>';
    box.appendChild(d);
  }
}

/** Shared by the Attach button and the auto-attach that follows Launch. */
function onAttached(r) {
  attached = true;
  setLink(true, `pid ${r.pid} · ${r.objects.toLocaleString()} objects · UE ${r.layout}`);
  $('attachHint').textContent = r.hwnd ? 'attached' : 'attached, but no game window for input';
  $('btnLaunch').disabled = false;

  // The offset checks take ~17s and arrive later as an event; attach no longer
  // waits on them, so show that they are still running rather than nothing.
  renderChecks(r.checks, r.checking);

  enable(['btnForce', 'btnReadRoll', 'btnScan', 'btnAll', 'btnVerify',
          'btnRestore', 'btnFindPlayer', 'oddsDenom', 'oddsSlider',
          'btnOddsApply', 'btnOddsRestore',
          'encOn', 'encShiny', 'encSearch', 'btnPartyScan',
          'moneyInput', 'btnMoneySet', 'btnMoneyRead'], true);
  refreshOdds();
  loadMoney().catch((e) => log('money: ' + (e.message || e), 'bad'));
  loadEncounter().catch(() => {});
  loadPokeRoster().catch(() => {});
  rpc('party.list', { rescan: false }).catch(() => {});
  $('btnOnce').disabled = !r.hwnd;
  $('btnHunt').disabled = !r.hwnd;
  log(`attached to pid ${r.pid}`, 'shiny');
  if (!r.player) log('looking for the player pawn in the background…');
  startPolling();
}

async function attach() {
  showError(null);
  const pid = Number($('pidSel').value);
  if (!pid) return showError(new Error('pick a running game process'));
  $('btnAttach').disabled = true;
  $('attachHint').textContent = 'scanning memory…';
  try {
    onAttached(await rpc('bot.attach', { pid, version: $('buildSel').value }));
  } catch (e) {
    attached = false;
    setLink(false, 'not attached');
    showError(e);
  } finally {
    $('btnAttach').disabled = false;
  }
}

function startPolling() {
  if (statusTimer) clearInterval(statusTimer);
  statusTimer = setInterval(async () => {
    if (!attached) return;
    try {
      const s = await rpc('bot.status');
      if (s.player) paintOdds(s.player.ShinyRate, s.player.odds, s.hunt && s.hunt.found);
      if (s.hunt) paintStats(s.hunt);
    } catch { /* transient; the next tick retries */ }
  }, 1000);
}

/* ------------------------------------------------------------------- odds */


async function readRoll() {
  showError(null);
  try {
    const r = await rpc('starter.state');
    if (!r.scene) { log(r.hint || 'starter screen is not open'); return null; }
    const s = r.scene;
    const mine = s['isShiny' + $('starterSel').selectedOptions[0].textContent + '?'];
    paintOdds(mine ? 0 : 1023, mine ? '1/1' : 'rolled', mine);
    $('oddsSub').innerHTML =
      `ShinyFrame <b>${s.ShinyFrame}</b> &nbsp;·&nbsp; BP_GE_PickStarterPlayer_C`;
    log(`roll: frame=${s.ShinyFrame} treecko=${s['isShinyTreecko?']} ` +
        `torchic=${s['isShinyTorchic?']} mudkip=${s['isShinyMudkip?']}`);
    return s;
  } catch (e) { showError(e); return null; }
}

$('btnReadRoll').onclick = readRoll;
$('btnForce').onclick = async () => {
  showError(null);
  try {
    const r = await rpc('starter.force', { starter: $('starterSel').value });
    log(`forced ${r.starter} shiny`, 'shiny');
    await readRoll();
  } catch (e) { showError(e); }
};

/* ------------------------------------------------------------------- hunt */
$('btnHunt').onclick = async () => {
  showError(null);
  if (hunting) {
    try { await rpc('hunt.stop'); } catch (e) { showError(e); }
    return;
  }
  const max = parseInt($('maxAttempts').value, 10);
  try {
    await rpc('hunt.start', {
      starter: $('starterSel').value,
      open_bag: $('openBag').checked,
      force_shiny: $('forceEachRun').checked,
      max_attempts: Number.isFinite(max) ? max : 0,
    });
  } catch (e) { showError(e); }
};

$('btnOnce').onclick = async () => {
  showError(null);
  $('btnOnce').disabled = true;
  try {
    const r = await rpc('hunt.attempt', {
      starter: $('starterSel').value,
      open_bag: $('openBag').checked,
    });
    log(`attempt finished — ${r.shiny ? 'SHINY' : 'not shiny'}`, r.shiny ? 'shiny' : null);
    paintStats(r.stats);
  } catch (e) { showError(e); } finally { $('btnOnce').disabled = false; }
};

$('btnFindPlayer').onclick = async () => {
  showError(null);
  try {
    const r = await rpc('bot.find_player');
    if (r.player) {
      paintOdds(r.player.ShinyRate, r.player.odds, false);
      log(`player ${r.player.class} at ${r.player.addr}`);
    } else {
      log('still no player pawn — load a save into the overworld first', 'bad');
    }
  } catch (e) { showError(e); }
};

function setHunting(on) {
  hunting = on;
  $('btnHunt').textContent = on ? 'Stop hunt' : 'Start hunt';
  $('btnHunt').dataset.running = String(on);
}

/* ----------------------------------------------------------------- cheats */
$('btnScan').onclick = async () => {
  showError(null);
  $('scanHint').textContent = 'scanning…';
  try {
    const r = await rpc('shiny.scan');
    $('scanHint').textContent = `${r.count} sites · currently ${r.odds}`;
    const wrap = $('sitesWrap');
    if (!r.sites.length) {
      wrap.innerHTML = '<div class="empty"><b>No sites found</b>The build may use a different constant.</div>';
      return;
    }
    const rows = r.sites.map((s) =>
      `<tr><td>${s.addr}</td><td>${s.value}</td><td>${s.cls}</td><td>${s.func}</td></tr>`).join('');
    wrap.innerHTML =
      `<table class="grid"><thead><tr><th>address</th><th>value</th><th>class</th><th>function</th></tr></thead>
       <tbody>${rows}</tbody></table>`;
    log(`shiny scan: ${r.count} sites at ${r.odds}`);
  } catch (e) { showError(e); $('scanHint').textContent = 'scan failed'; }
};

$('btnAll').onclick = async () => {
  try { const r = await rpc('shiny.set', { probability: 1.0 });
        log(`patched ${r.patched} sites → ${r.odds}`, 'shiny'); } catch (e) { showError(e); }
};
$('btnRestore').onclick = async () => {
  try { const r = await rpc('shiny.restore'); log(`restored ${r.restored} sites`); }
  catch (e) { showError(e); }
};
$('btnVerify').onclick = async () => {
  try { const r = await rpc('shiny.verify');
        log(`write test ${r.ok ? 'passed' : 'FAILED'} — ${r.detail}`, r.ok ? null : 'bad'); }
  catch (e) { showError(e); }
};

/* --------------------------------------------- encounter / party / money */
// Filled from encounter.species once attached -- the game's own species
// database. It used to be nine hardcoded entries plus whatever the Pokedex had
// revealed, which is why picking a sprite still left the dex number to type.
let DEX_BY_NAME = {};
let encSel = { name: '', dex: 0 };
const frontCache = new Map();
let POKE_ROSTER = [];
let encObs = null;

async function fillFront(img, name, shiny, anim) {
  if (!img || !name || /^species-/i.test(name)) {
    if (img) img.hidden = true;
    return;
  }
  const key = name.toLowerCase() + (shiny ? '|s' : '') + (anim ? '|gif' : '|still');
  try {
    let uri = frontCache.get(key);
    if (!uri) {
      const r = await rpc('assets.front', { name, shiny: !!shiny, still: !anim });
      uri = r.uri;
      frontCache.set(key, uri);
    }
    img.src = uri;
    img.hidden = false;
  } catch {
    img.hidden = true;
  }
}

async function loadPokeRoster() {
  if (!POKE_ROSTER.length) {
    $('encGrid').innerHTML = '<div class="empty tight">Reading Pokémon sprites…</div>';
    try {
      const r = await rpc('assets.roster', {});
      POKE_ROSTER = r.pokemon || [];
    } catch (e) {
      $('encGrid').innerHTML = '<div class="empty tight">Could not read sprites from the pak.</div>';
      throw e;
    }
  }
  $('encSearch').disabled = false;
  paintEncGrid($('encSearch').value);
}

function paintEncGrid(query) {
  const q = (query || '').trim().toLowerCase();
  const box = $('encGrid');
  const rows = q
    ? POKE_ROSTER.filter((p) => p.name.toLowerCase().includes(q))
    : POKE_ROSTER;
  if (!rows.length) {
    box.innerHTML = '<div class="empty tight">No Pokémon match that name.</div>';
    return;
  }
  box.innerHTML = '';
  const want = (encSel.name || '').toLowerCase();
  const known = Object.keys(DEX_BY_NAME).length > 0;
  rows.forEach((p) => {
    const b = document.createElement('button');
    const dex = DEX_BY_NAME[p.name.toLowerCase()] || 0;
    b.type = 'button';
    b.className = 'enc-tile';
    b.dataset.name = p.name;
    // A sprite exists for more species than this build has data for. Saying so
    // on the tile beats letting someone pick one and hit an error.
    b.dataset.unavailable = String(known && !dex);
    if (known && !dex) b.title = 'No species data in this build';
    b.setAttribute('aria-pressed', String(p.name.toLowerCase() === want));
    b.innerHTML = `<img alt="" hidden><span>${p.name}</span>` +
                  (dex ? `<i class="dex">#${dex}</i>` : '');
    b.onclick = () => selectEnc(p.name);
    box.appendChild(b);
  });
  if (encObs) encObs.disconnect();
  encObs = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      encObs.unobserve(en.target);
      const name = en.target.dataset.name;
      fillFront(en.target.querySelector('img'), name, false, false);
    });
  }, { root: box, rootMargin: '80px' });
  box.querySelectorAll('.enc-tile').forEach((t) => encObs.observe(t));
}

function selectEnc(name) {
  const dex = DEX_BY_NAME[name.toLowerCase()] || 0;
  encSel = { name, dex };
  $('encSelName').textContent = name;
  const badge = $('encDex');
  badge.hidden = !dex;
  badge.textContent = dex ? '#' + dex : '';
  $('encOn').disabled = !dex;
  // "no species data" is only true once the list has actually loaded. Saying it
  // while detached blamed the game for the app not being attached yet.
  const loaded = Object.keys(DEX_BY_NAME).length > 0;
  $('encHint').textContent = dex
    ? `${name} is ready. Flip the switch to force the next encounter.`
    : loaded
      ? `${name} has no species data in this build, so it cannot be forced.`
      : 'Attach to the game to load dex numbers.';
  document.querySelectorAll('.enc-tile').forEach((t) =>
    t.setAttribute('aria-pressed', String(t.dataset.name === name)));
  const img = $('encPreview');
  fillFront(img, name, $('encShiny').checked, true);
}

$('encSearch').oninput = () => paintEncGrid($('encSearch').value);

async function loadEncounter() {
  if (!attached) return;
  // The dex numbers come from the game, so a pick is a complete answer.
  try {
    const s = await rpc('encounter.species');
    DEX_BY_NAME = {};
    (s.species || []).forEach((x) => { DEX_BY_NAME[x.name.toLowerCase()] = x.dex; });
    paintEncGrid($('encSearch').value);
  } catch (e) {
    log('could not read the species list: ' + e.message);
  }

  const r = await rpc('encounter.status');
  (r.known || []).forEach((k) => { DEX_BY_NAME[k.id.toLowerCase()] = k.dex; });
  $('encOn').checked = !!r.enabled;
  $('encCard').dataset.on = String(!!r.enabled);
  if (r.label) {
    encSel = { name: r.label, dex: r.dex || DEX_BY_NAME[r.label.toLowerCase()] || 0 };
    $('encSelName').textContent = r.label;
    $('encDex').hidden = !encSel.dex;
    $('encDex').textContent = encSel.dex ? '#' + encSel.dex : '';
    fillFront($('encPreview'), r.label, !!r.shiny, true);
  }
  $('encShiny').checked = !!r.shiny;
  $('encOn').disabled = !encSel.dex;
  $('encHint').textContent = r.enabled
    ? `Hooked: ${r.label || r.dex}${r.shiny ? ' · shiny' : ''}. Turn off after the catch.`
    : (encSel.dex ? `${encSel.name} is ready. Flip the switch to force the next encounter.`
                  : 'Pick a Pokémon, then flip the switch.');
}

$('encOn').onchange = async () => {
  showError(null);
  if (!$('encOn').checked) {
    try {
      await rpc('encounter.clear');
      $('encCard').dataset.on = 'false';
      $('encHint').textContent = 'Hook cleared. Wild encounters are back to normal.';
      log('encounter hook cleared');
    } catch (e) {
      $('encOn').checked = true;
      showError(e);
    }
    return;
  }
  if (!encSel.dex) {
    $('encOn').checked = false;
    return showError(new Error('pick a Pokémon first'));
  }
  $('encHint').textContent = `Hooking ${encSel.name}…`;
  try {
    await rpc('encounter.set', {
      enabled: true,
      dex: encSel.dex,
      name: encSel.name,
      shiny: $('encShiny').checked,
    });
  } catch (e) {
    $('encOn').checked = false;
    showError(e);
  }
};

$('encShiny').onchange = () => {
  const name = encSel.name;
  if (name) fillFront($('encPreview'), name, $('encShiny').checked, true);
  if ($('encOn').checked) $('encOn').onchange();
};

$('btnPartyScan').onclick = async () => {
  showError(null);
  $('partyHint').textContent = 'Scanning heap for a validated party TArray…';
  try { await rpc('party.list', { rescan: true }); }
  catch (e) { showError(e); }
};

function paintPartyBar(slots) {
  const bar = $('partyBar');
  if (!slots || !slots.length) {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  bar.hidden = false;
  bar.innerHTML = '';
  slots.forEach((s) => {
    const d = document.createElement('button');
    d.type = 'button';
    d.className = 'slot';
    d.dataset.shiny = String(!!s.shiny);
    d.title = `${s.name} · Lv.${s.level}`;
    d.innerHTML = '<img alt="" hidden>';
    d.onclick = () => document.querySelector('nav button[data-pane="cheats"]').click();
    bar.appendChild(d);
    fillFront(d.querySelector('img'), s.name, s.shiny, true);
  });
}

function paintParty(r) {
  const box = $('partySlots');
  const slots = r.slots || [];
  paintPartyBar(slots);
  if (!slots.length) {
    box.innerHTML = '<div class="empty tight">No party found. Load a save that has Pokemon.</div>';
    return;
  }
  box.innerHTML = '';
  slots.forEach((s) => {
    const row = document.createElement('div');
    row.className = 'pslot';
    row.dataset.shiny = String(!!s.shiny);
    row.innerHTML =
      `<img class="pspr" alt="" hidden>` +
      `<span class="n">${s.slot}</span>` +
      `<span class="who">${s.name}</span>` +
      `<span class="lv">Lv.${s.level}</span>` +
      `<label class="sw" title="Shiny"><input type="checkbox" ${s.shiny ? 'checked' : ''}>` +
      `<span class="track"><i></i></span></label>`;
    fillFront(row.querySelector('img'), s.name, s.shiny, true);
    const inp = row.querySelector('input');
    inp.onchange = async () => {
      showError(null);
      $('partyHint').textContent = `Writing ${s.name} ${inp.checked ? 'shiny' : 'normal'}…`;
      try {
        await rpc('party.set_shiny', { slot: s.slot, shiny: inp.checked });
      } catch (e) {
        inp.checked = !inp.checked;
        showError(e);
      }
    };
    box.appendChild(row);
  });
  $('partyHint').textContent = `${slots.length} Pokemon. Reopen the party card after a change, then save.`;
}

function setGauge(state, sub) {
  $('gaugeState').textContent = state;
  $('gaugeSub').textContent = sub || '';
}

function pushAttempt(n, who, shiny) {
  const list = $('attempts');
  if (list.querySelector('.empty')) list.innerHTML = '';
  const row = document.createElement('div');
  row.className = 'att';
  row.dataset.shiny = String(!!shiny);
  row.innerHTML = `<span class="n">#${n}</span><span class="who">${who}</span>` +
                  `<span class="res">${shiny ? 'SHINY' : 'no'}</span>`;
  list.insertBefore(row, list.firstChild);
  while (list.children.length > 200) list.removeChild(list.lastChild);
}

/* ------------------------------------------------------------------ events */
window.gamma.onEvent((msg) => {
  const { event, data } = msg;
  if (event === 'ready') { log(`daemon ready (python ${data.python})`); refreshProcesses(); return; }
  if (event === 'exit') { setLink(false, 'daemon stopped'); log('daemon exited', 'bad'); return; }
  if (event === 'log') { log(data.line); return; }
  if (event === 'status') { log(data.stage); return; }
  if (event === 'extract') { onExtractProgress(data); return; }
  if (event === 'checks') { renderChecks(data.checks, false); return; }
  if (event === 'odds') { paintOddsState(data); return; }
  if (event === 'encounter') {
    if (data.kind === 'working') {
      $('encHint').textContent = 'Finding the species database… first time can take about a minute.';
      return;
    }
    if (data.kind === 'error') {
      $('encOn').checked = false;
      $('encCard').dataset.on = 'false';
      $('encHint').textContent = data.error;
      showError(new Error(data.error));
      return;
    }
    if (data.kind === 'done') {
      $('encOn').checked = true;
      $('encCard').dataset.on = 'true';
      $('encHint').textContent =
        `Hooked dex ${data.dex}${data.shiny ? ' · shiny' : ''}. Trigger a wild encounter, then turn this off.`;
      log(`encounter hooked dex=${data.dex} shiny=${data.shiny}`, 'shiny');
    }
    return;
  }
  if (event === 'party') {
    if (data.kind === 'working') {
      $('partyHint').textContent = 'Scanning…';
      return;
    }
    if (data.kind === 'error') {
      $('partyHint').textContent = data.error;
      showError(new Error(data.error));
      return;
    }
    if (data.kind === 'done') {
      paintParty(data);
      if (data.name) log(`${data.name} → ${data.shiny ? 'shiny' : 'normal'} (${data.copies} copies)`);
    }
    return;
  }
  // The heap-scan money workflow is gone -- money is read straight off
  // ItemInventorySystem now -- but the daemon still exposes the old scan RPCs,
  // so surface their errors rather than dropping them silently.
  if (event === 'money') {
    if (data.kind === 'error') showError(new Error(data.error));
    return;
  }
  if (event === 'attached') {
    $('btnLaunch').disabled = false;
    if (data.error) { showError(new Error(data.error)); $('attachHint').textContent = data.error; }
    else { onAttached(data); refreshProcesses(); }
    return;
  }
  if (event === 'player') { $('attachHint').textContent = 'attached · player found'; return; }
  if (event === 'hunt') {
    const st = data.stats || {};
    paintStats(st);
    switch (data.kind) {
      case 'start': setHunting(true); setGauge('hunting', `target ${st.target}`);
        log(`hunt started — target ${st.target}, odds ${st.odds}`, 'shiny'); break;
      case 'attempt': log(`#${st.attempts} ${st.status}`); setGauge('hunting', st.status); break;
      case 'result':
        log(`#${st.attempts} ${data.shiny ? 'SHINY' : 'not shiny'}`, data.shiny ? 'shiny' : null);
        pushAttempt(st.attempts, data.starter || st.target || '', data.shiny);
        setGauge(data.shiny ? 'SHINY' : 'no shiny', `attempt ${st.attempts}`);
        break;
      case 'found': setHunting(false); setGauge('FOUND', `attempt ${st.found_at}`);
        log(`FOUND a shiny on attempt ${st.found_at}`, 'shiny'); break;
      case 'reset': log('soft reset'); break;
      case 'timeout': log(`timed out waiting for ${data.waiting_for}`, 'bad'); break;
      case 'error': setHunting(false); log(`hunt error: ${data.error}`, 'bad'); break;
      case 'done': setHunting(false); log('hunt stopped'); break;
      case 'odds': log(`odds now ${st.odds}`); break;
    }
  }
});

/* -------------------------------------------------------------------- init */
$('btnRefresh').onclick = refreshProcesses;
$('btnAttach').onclick = attach;
$('resetHint').innerHTML =
  'A dud is rejected without accepting a Pokemon, then <b>SHIFT+R</b> soft-resets ' +
  'to re-roll — about <b>30s per reset (~120/hour)</b>, nearly all of it the ' +
  'game&rsquo;s own load. The game does not need focus. Tick <b>force on every ' +
  'attempt</b> to prove the loop instantly.';
loadBuilds().catch(showError);
loadStarters().catch(showError);


/* dev only: --autohunt=<starter> attaches and runs a forced hunt unattended, so a
   screenshot can show real numbers instead of an empty panel. */
(async () => {
  const want = new URLSearchParams(location.search).get('autohunt');
  if (!want) return;
  log('autohunt: ' + want);
  await new Promise((r) => setTimeout(r, 3000));
  try {
    await refreshProcesses();
    log('autohunt: attaching');
    await attach();
    if (!attached) { log('autohunt: attach failed', 'bad'); return; }
    selectStarter(want);
    $('forceEachRun').checked = true;
    await new Promise((r) => setTimeout(r, 1200));
    log('autohunt: starting hunt');
    const r = await rpc('hunt.start', { starter: want, open_bag: true,
                                        force_shiny: true, max_attempts: 1 });
    log('autohunt: hunt.start -> ' + JSON.stringify(r));
  } catch (e) {
    log('autohunt failed: ' + (e.message || e), 'bad');
  }
})();

/* ------------------------------------------------------------- odds control
   One number — "1 in N" — reachable three ways: type it, drag it, or pick a
   preset. They all write the same state, so they can never disagree.

   The slider is LOG scaled. A linear 1..1,000,000 slider spends 99% of its
   travel above 1/10,000, which makes every interesting value (1/512, 1/1024,
   1/4096) land in the first pixel — that is why dragging felt broken. */
const ODDS_MIN = 1, ODDS_MAX = 1000000, ODDS_STEPS = 1000;

const denomToSlider = (d) => Math.round(
  (Math.log(Math.min(ODDS_MAX, Math.max(ODDS_MIN, d))) / Math.log(ODDS_MAX)) * ODDS_STEPS);
const sliderToDenom = (v) => {
  const raw = Math.exp((v / ODDS_STEPS) * Math.log(ODDS_MAX));
  // snap to something a person would type
  const nice = [1, 2, 4, 8, 16, 32, 64, 100, 128, 256, 512, 1000, 1024, 2048,
                4096, 8192, 10000, 16384, 32768, 65536, 100000, 262144, 1000000];
  let best = nice[0];
  for (const n of nice) if (Math.abs(Math.log(n) - Math.log(raw)) < Math.abs(Math.log(best) - Math.log(raw))) best = n;
  return Math.abs(Math.log(best) - Math.log(raw)) < 0.06 ? best : Math.round(raw);
};

let oddsDenom = 4096;

function paintOddsControl(d, { fromInput = false } = {}) {
  oddsDenom = Math.max(ODDS_MIN, Math.min(ODDS_MAX, Math.round(d) || 1));
  if (!fromInput) $('oddsDenom').value = String(oddsDenom);
  $('oddsSlider').value = String(denomToSlider(oddsDenom));
  document.querySelectorAll('#oddsPresets .chip').forEach((c) => {
    c.setAttribute('aria-pressed', String(Number(c.dataset.denom) === oddsDenom));
  });
  paintOdds(oddsDenom - 1, oddsDenom === 1 ? '1/1 (forced)' : `1/${oddsDenom.toLocaleString()}`, false);
}

$('oddsSlider').oninput = () => paintOddsControl(sliderToDenom(Number($('oddsSlider').value)));
$('oddsDenom').oninput = () => {
  const v = Number($('oddsDenom').value);
  if (Number.isFinite(v) && v >= 1) paintOddsControl(v, { fromInput: true });
};
$('oddsPresets').onclick = (ev) => {
  const c = ev.target.closest('.chip');
  if (!c) return;
  paintOddsControl(Number(c.dataset.denom));
  applyOdds().catch(showError);
};

async function applyOdds() {
  if (!attached) return;
  $('btnOddsApply').disabled = true;
  $('oddsHint').textContent = `Setting 1/${oddsDenom.toLocaleString()}…`;
  let r;
  try {
    r = await rpc('odds.set', { denominator: oddsDenom });
  } finally {
    $('btnOddsApply').disabled = false;
  }
  log(`odds set to ${r.text} — ${r.patched} constants${r.rate_set ? ' + ShinyRate' : ''}`);
  $('oddsHint').innerHTML =
    `Now <b>${r.text}</b> — patched <b>${r.patched}</b> Blueprint constant${r.patched === 1 ? '' : 's'}` +
    `${r.rate_set ? ' and <code>ShinyRate</code>' : ''}. Wild encounters only.`;
}

$('btnOddsApply').onclick = () => applyOdds().catch(showError);
$('btnOddsRestore').onclick = async () => {
  try {
    const r = await rpc('shiny.restore');
    log(`restored ${r.restored} constants`);
    await refreshOdds();
  } catch (e) { showError(e); }
};

async function refreshOdds() {
  if (!attached) return;
  try {
    paintOddsState(await rpc('odds.get'));
  } catch (e) { /* not scanned yet; leave the default copy */ }
}

/* The Blueprint scan takes ~37s and runs in the background, so the card says so
   rather than showing a stale number as if it were current. */
function paintOddsState(r) {
  if (!r) return;
  if (r.scanning) {
    $('oddsHint').textContent =
      'Reading the Blueprint odds constants… the controls work as soon as it finishes.';
    return;
  }
  if (r.denominator) paintOddsControl(r.denominator);
  $('oddsHint').innerHTML =
    `Currently <b>${r.text}</b> across <b>${r.sites}</b> Blueprint site${r.sites === 1 ? '' : 's'}. ` +
    'Wild encounters only — the starter is the button below.';
}

paintOddsControl(4096);

/* ------------------------------------------------------------- game path
   The built-in specs hardcode one machine's paths. Pointing at the exe makes
   the whole app work on any install, and the choice is remembered so it is
   asked once, not every session. */
async function loadGamePath() {
  const saved = await window.gamma.getSettings();
  if (!saved.gameExe) return;
  try {
    paintGamePath(await rpc('app.set_game_path', { exe: saved.gameExe }));
    loadPokeRoster().catch(() => {});
  } catch (e) {
    $('gamePath').textContent = `Saved game path is gone: ${saved.gameExe}`;
  }
}

function paintGamePath(info) {
  if (!info || !info.exe) {
    $('gamePath').textContent = 'No game chosen yet.';
    return;
  }
  const key = info.container === 'pak'
    ? (info.has_key ? ' · key ready' : ' · NO AES KEY') : '';
  $('gamePath').innerHTML =
    `<b>${info.name}</b><br><span class="path">${info.exe}</span>` +
    `<br>${info.container}${key}`;
}

$('btnPickExe').onclick = async () => {
  try {
    const exe = await window.gamma.pickExe();
    if (!exe) return;
    await window.gamma.setSettings({ gameExe: exe });
    const info = await rpc('app.set_game_path', { exe });
    paintGamePath(info);
    log(`game set to ${info.exe || exe}`);
    POKE_ROSTER = [];
    frontCache.clear();
    loadPokeRoster().catch(() => {});
    if (info.warning) log(info.warning);
    try { await loadBuilds(); } catch (e) { /* dropdown is optional */ }
    await refreshProcesses();
  } catch (e) { showError(e); }
};

$('btnLaunch').onclick = async () => {
  $('btnLaunch').disabled = true;
  $('attachHint').textContent = 'launching the game…';
  try {
    const saved = await window.gamma.getSettings();
    const r = await rpc('app.launch', { attach: true, exe: saved.gameExe });
    log(`launched ${r.launched}`);
    $('attachHint').textContent = 'waiting for the game to finish booting…';
  } catch (e) {
    showError(e);
    $('btnLaunch').disabled = false;
  }
};

loadGamePath().catch(() => {});

/* ------------------------------------------------------------- save backups */
/* Restoring overwrites the live save and there is no undo the user can reach,
   so every destructive action goes through confirm() below. */
function confirmAction({ title, body, note, ok }) {
  return new Promise((resolve) => {
    $('modalTitle').textContent = title;
    $('modalBody').textContent = body;
    const n = $('modalNote');
    n.hidden = !note;
    n.textContent = note || '';
    $('modalOk').textContent = ok || 'Confirm';
    $('modal').hidden = false;
    $('modalOk').focus();

    const done = (answer) => {
      $('modal').hidden = true;
      $('modalOk').onclick = null;
      $('modalCancel').onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(answer);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') done(false);
      if (e.key === 'Enter') done(true);
    };
    $('modalOk').onclick = () => done(true);
    $('modalCancel').onclick = () => done(false);
    document.addEventListener('keydown', onKey);
  });
}

function fmtBytes(n) {
  if (!n) return '—';
  return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
       : n >= 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';
}

function fmtWhen(epochSeconds) {
  if (!epochSeconds) return '—';
  return new Date(epochSeconds * 1000).toLocaleString([], {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

async function loadSaves() {
  let info;
  try {
    info = await rpc('saves.info');
  } catch (e) { return showError(e); }
  $('saveDir').textContent = info.dir;
  $('saveDir').title = info.dir;
  $('saveWhen').textContent = fmtWhen(info.modified);
  $('saveSize').textContent = info.exists
    ? `${info.files} file${info.files === 1 ? '' : 's'} · ${fmtBytes(info.bytes)}`
    : 'no save found';

  const warn = $('saveWarn');
  if (!info.exists) {
    warn.hidden = false;
    warn.textContent = 'No save folder yet. Play and save once, then back up.';
  } else if (info.game_running) {
    warn.hidden = false;
    warn.textContent = 'The game is running. Close it before restoring: it '
      + 'holds the save files open, and it writes its own copy back at the next '
      + 'save point. Backing up now is fine.';
  } else {
    warn.hidden = true;
  }
  $('btnBackup').disabled = !info.exists;
  await paintBackups();
}

async function paintBackups() {
  const box = $('backupList');
  let list = [];
  try {
    list = (await rpc('saves.list')).backups || [];
  } catch (e) { return showError(e); }

  if (!list.length) {
    box.innerHTML = '<div class="empty tight">No backups yet.</div>';
    return;
  }
  box.textContent = '';
  list.forEach((b) => {
    const row = document.createElement('div');
    row.className = 'backup';
    row.dataset.auto = String(b.auto);

    const left = document.createElement('div');
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = b.label;
    if (b.auto) {
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = 'auto';
      tag.title = 'Taken automatically before a restore';
      nm.appendChild(tag);
    }
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = `${fmtWhen(b.created)} · ${b.files} files · ${fmtBytes(b.bytes)}`;
    left.append(nm, sub);

    const row2 = document.createElement('div');
    row2.className = 'row';
    const restore = document.createElement('button');
    restore.className = 'act';
    restore.textContent = 'Restore';
    restore.onclick = () => doRestore(b);
    const del = document.createElement('button');
    del.className = 'act';
    del.dataset.tone = 'danger';
    del.textContent = 'Delete';
    del.onclick = () => doDelete(b);
    row2.append(restore, del);

    row.append(left, row2);
    box.appendChild(row);
  });
}

async function doRestore(b) {
  const running = !$('saveWarn').hidden
    && $('saveWarn').textContent.startsWith('The game is running');
  const yes = await confirmAction({
    title: 'Restore this save?',
    body: `This replaces your current save with “${b.label}” from `
        + `${fmtWhen(b.created)}. Your current save is backed up first, but any `
        + `progress since your last in-game save is lost.`,
    note: running
      ? 'The game is running. The restore may be refused outright, and if it '
        + 'goes through, the game writes its own save back over it at the next '
        + 'save point. Close the game first.'
      : '',
    ok: 'Restore',
  });
  if (!yes) return;
  try {
    const r = await rpc('saves.restore', { id: b.id });
    log(`restored save “${b.label}” (${r.files} files)`);
    $('saveHint').textContent = r.safety_backup
      ? `Restored. Your previous save was kept as a backup.`
      : 'Restored.';
    await loadSaves();
  } catch (e) { showError(e); }
}

async function doDelete(b) {
  const yes = await confirmAction({
    title: 'Delete this backup?',
    body: `“${b.label}” from ${fmtWhen(b.created)} will be deleted. This cannot `
        + `be undone. Your live save is not touched.`,
    ok: 'Delete',
  });
  if (!yes) return;
  try {
    await rpc('saves.delete', { id: b.id });
    log(`deleted backup “${b.label}”`);
    await loadSaves();
  } catch (e) { showError(e); }
}

$('btnBackup').onclick = async () => {
  showError(null);
  const label = $('backupLabel').value;
  $('btnBackup').disabled = true;
  try {
    const r = await rpc('saves.create', { label });
    $('backupLabel').value = '';
    $('saveHint').textContent = `Backed up ${r.files} files as “${r.label}”.`;
    log(`save backed up: ${r.label}`);
    await loadSaves();
  } catch (e) {
    showError(e);
  } finally {
    $('btnBackup').disabled = false;
  }
};

/* ------------------------------------------------------------------ money */
/* The money the game actually spends is ItemInventorySystem.Money -- see the
   note in money.py for the two decoy fields that look right and are not. */
async function loadMoney() {
  if (!attached) {
    $('moneyNow').textContent = '—';
    $('moneyHint').textContent = 'Attach on the Hunt tab to read your money.';
    enable(['moneyInput', 'btnMoneySet', 'btnMoneyRead'], false);
    return;
  }
  enable(['moneyInput', 'btnMoneySet', 'btnMoneyRead'], true);
  // The first read after attaching builds the daemon's class index and takes a
  // few seconds; without this the card just sits on a dash looking broken.
  if ($('moneyNow').textContent === '—') $('moneyHint').textContent = 'Reading…';
  try {
    const r = await rpc('money.get');
    $('moneyNow').textContent = r.found ? r.money.toLocaleString() : '—';
    $('moneyHint').textContent = r.found
      ? 'Spending it in a shop updates straight away.'
      : 'No money found — load a save first, then refresh.';
  } catch (e) {
    $('moneyHint').textContent = 'Could not read money: ' + (e.message || e);
    throw e;
  }
}

$('btnMoneyRead').onclick = () => loadMoney().catch(showError);

$('btnMoneySet').onclick = async () => {
  showError(null);
  const amount = Number($('moneyInput').value);
  if (!Number.isFinite(amount) || amount < 0) {
    return showError(new Error('enter an amount first'));
  }
  try {
    const r = await rpc('money.set', { amount });
    $('moneyNow').textContent = (r.money ?? 0).toLocaleString();
    $('moneyHint').textContent = `Was ${(r.before ?? 0).toLocaleString()}, now `
      + `${(r.money ?? 0).toLocaleString()}.`;
    log(`money set to ${r.money}`);
    $('moneyInput').value = '';
  } catch (e) { showError(e); }
};
