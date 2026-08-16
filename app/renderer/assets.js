'use strict';
/* The Assets tab.
   Browsing is free: the daemon reads the pak index once and decodes a file only
   when it is actually shown. That index costs ~2s, so it is read the first time
   someone opens this tab rather than at launch. Nothing is written to disk until
   Extract is pressed. */

const A = {
  ready: false, cat: null, subject: null, entry: null,
  entries: [], zoom: 4, seq: 0, extracting: false,
};

const fmtCount = (n) =>
  (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n));

const KIND_TAG = { animation: 'GIF', audio: 'MP3', image: 'PNG', raw: 'RAW' };
const KIND_LABEL = { animation: 'Animation', image: 'Sprite',
                     audio: 'Sound', raw: 'Package' };

function assetBusy(on) {
  document.querySelector('#pane-assets .stage').classList.toggle('busy', on);
}

function assetHint(text) { $('assetHint').textContent = text; }

/* -------------------------------------------------------------- categories */
async function assetsInit() {
  if (A.ready) return;
  A.ready = true;
  assetHint('reading the pak index…');
  try {
    const { categories } = await rpc('assets.categories', {});
    const box = $('cats');
    box.textContent = '';
    let total = 0;
    categories.forEach((c) => {
      total += c.count;
      const b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = `<span>${c.label}</span><span class="n">${fmtCount(c.count)}</span>`;
      b.onclick = () => pickCategory(c, b).catch(showError);
      box.appendChild(b);
    });
    $('libFoot').textContent = fmtCount(total) + ' files';
    $('assetSearch').disabled = false;
    assetHint('Nothing is written to disk until you press Extract.');
    if (categories[0]) await pickCategory(categories[0], box.firstChild);
  } catch (e) {
    A.ready = false;
    $('cats').innerHTML = '<div class="empty">Could not read the pak.</div>';
    assetHint('could not open the game container');
    showError(e);
  }
}

async function pickCategory(cat, btn) {
  A.cat = cat;
  // picking a library means "show me this", so an active search is dropped
  $('assetSearch').value = '';
  document.querySelectorAll('#cats button').forEach((b) => {
    b.setAttribute('aria-current', String(b === btn));
  });
  await loadSubjects();
}

/* ---------------------------------------------------------------- subjects */
let searchTimer = null;
function onSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadSubjects().catch(showError), 180);
}

async function loadSubjects() {
  const q = $('assetSearch').value.trim();
  // Typing searches the WHOLE library, not the selected category. "Fisherman" is
  // an NPC, not a Pokemon, and searching one category at a time just told you it
  // did not exist.
  const { subjects, total } = q
    ? await rpc('assets.search', { query: q, limit: 400 })
    : (A.cat ? await rpc('assets.subjects',
                         { category: A.cat.id, query: '', limit: 600 })
             : { subjects: [], total: 0 });

  // during a search the results span libraries, so leaving one highlighted in the
  // rail would be a lie about what is on screen
  if (q) {
    document.querySelectorAll('#cats button').forEach((b) => {
      b.setAttribute('aria-current', 'false');
    });
  }

  const box = $('subjects');
  box.textContent = '';
  if (!subjects.length) {
    const p = document.createElement('div');
    p.className = 'empty tight';
    p.textContent = q ? `Nothing in the game matches “${q}”.`
                      : 'Pick a library.';
    box.appendChild(p);
    $('subjFoot').textContent = q ? 'no matches' : '';
    A.entries = [];
    $('entries').textContent = '';
    $('stageName').textContent = q ? `No match for “${q}”` : 'Nothing selected';
    $('stageKind').textContent = 'Preview';
    $('btnExtractAll').disabled = true;
    clearStage('No match', 'Try another name, or pick a library on the left.');
    return;
  }
  subjects.forEach((s) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = s.name;
    if (s.group) {
      const g = document.createElement('span');
      g.className = 'grp';
      g.textContent = s.group;
      b.appendChild(g);
    }
    b.onclick = () => pickSubject(s, b).catch(showError);
    box.appendChild(b);
  });
  $('subjFoot').textContent = q
    ? `${subjects.length} match${subjects.length === 1 ? '' : 'es'}`
    : `${subjects.length} of ${total}`;
  await pickSubject(subjects[0], box.firstChild);
}

/* ----------------------------------------------------------------- entries */
async function pickSubject(s, btn) {
  A.subject = s;
  document.querySelectorAll('#subjects button').forEach((b) => {
    b.setAttribute('aria-current', String(b === btn));
  });

  // a sound is its own subject: there is nothing to list, just play it
  if (s.kind === 'audio') {
    A.entries = [{ kind: 'audio', id: s.id, name: s.name }];
  } else {
    const { entries } = await rpc('assets.entries', { dir: s.id });
    A.entries = entries;
  }

  const box = $('entries');
  box.textContent = '';
  A.entries.forEach((e) => {
    const b = document.createElement('button');
    b.type = 'button';
    const k = document.createElement('span');
    k.className = 'k';
    k.textContent = KIND_TAG[e.kind] || 'RAW';
    b.appendChild(k);
    b.appendChild(document.createTextNode(e.name));
    if (e.count > 1) {
      const f = document.createElement('span');
      f.className = 'k';
      f.style.marginLeft = '6px';
      f.textContent = e.count + 'f';
      b.appendChild(f);
    }
    b.onclick = () => showEntry(e, b).catch(showError);
    box.appendChild(b);
  });

  $('btnExtractAll').disabled = !A.entries.length || A.extracting;
  $('btnExtractAll').textContent = A.entries.length > 1
    ? `Extract all ${A.entries.length}` : 'Extract all';

  // Open on the plain idle when there is one. Alphabetical order puts
  // "TreeckoShiny_Front_Cry" first, which is nobody's idea of the default view.
  const playable = A.entries.filter((e) => e.kind !== 'raw');
  const first = playable.find((e) => /idle/i.test(e.name) && !/shiny/i.test(e.name))
             || playable.find((e) => !/shiny/i.test(e.name))
             || playable[0];
  if (first) {
    await showEntry(first, box.children[A.entries.indexOf(first)]);
  } else if (A.entries.length) {
    clearStage(s.name, 'Meshes and Blueprints have no preview — extract them as-is.');
    $('btnExtractOne').disabled = true;
  } else {
    clearStage(s.name, 'Nothing in this folder.');
  }
}

/* ------------------------------------------------------------------- stage */
function clearStage(title, why) {
  A.entry = null;
  $('stageImg').hidden = true;
  $('stageImg').removeAttribute('src');
  $('stageEmpty').hidden = false;
  $('stageEmpty').textContent = '';
  const b = document.createElement('b');
  b.textContent = title;
  $('stageEmpty').appendChild(b);
  $('stageEmpty').appendChild(document.createTextNode(why));
  $('zoomer').hidden = true;
  $('stageMeta').textContent = '';
  $('btnPlay').hidden = true;
  $('btnExtractOne').disabled = true;
}

async function showEntry(e, btn) {
  A.entry = e;
  document.querySelectorAll('#entries button').forEach((b) => {
    b.setAttribute('aria-current', String(b === btn));
  });
  const sub = A.subject ? A.subject.name : '';
  $('stageName').textContent =
    e.name && e.name !== sub && e.name !== '.' ? `${sub} · ${e.name}` : sub;
  $('stageKind').textContent = KIND_LABEL[e.kind] || 'Asset';
  $('btnExtractOne').disabled = A.extracting;
  $('btnPlay').hidden = e.kind !== 'audio';

  if (e.kind === 'raw') {
    clearStage(e.name, 'A mesh or Blueprint — extract the raw package.');
    $('btnExtractOne').disabled = A.extracting;
    return;
  }

  const seq = ++A.seq;
  assetBusy(true);
  $('stageMeta').textContent = 'decoding…';
  try {
    const r = await rpc('assets.preview', { kind: e.kind, id: e.id });
    if (seq !== A.seq) return;              // a later click already won
    if (e.kind === 'audio') {
      clearStage(e.name, 'Playing. Press Play to hear it again.');
      $('btnPlay').hidden = false;
      $('btnPlay').disabled = false;
      $('btnExtractOne').disabled = A.extracting;
      const a = $('stageAudio');
      a.src = r.uri;
      a.play().catch(() => {});
      $('stageMeta').textContent = 'MP3';
    } else {
      const img = $('stageImg');
      img.src = r.uri;
      img.hidden = false;
      $('stageEmpty').hidden = true;
      $('zoomer').hidden = false;
      applyZoom();
      $('stageMeta').textContent = e.kind === 'animation'
        ? `${r.frames} frame${r.frames === 1 ? '' : 's'} · ${r.fps || '?'} fps · GIF`
        : 'PNG';
    }
  } catch (err) {
    if (seq === A.seq) clearStage(e.name, String(err.message || err));
  } finally {
    if (seq === A.seq) assetBusy(false);
  }
}

/* sprites are 128px and must not be smoothed; scale by whole pixels only */
function applyZoom() {
  const img = $('stageImg');
  const size = () => {
    if (!img.naturalWidth) return;
    img.style.width = (img.naturalWidth * A.zoom) + 'px';
    img.style.height = (img.naturalHeight * A.zoom) + 'px';
  };
  img.onload = size;
  if (img.complete) size();
}

/* ---------------------------------------------------------------- extract */
async function extract(items) {
  const dir = await window.gamma.pickFolder();
  if (!dir) return;
  A.extracting = true;
  $('btnExtractOne').disabled = true;
  $('btnExtractAll').disabled = true;
  assetHint(`extracting ${items.length}…`);
  try {
    await rpc('assets.extract', { items, out_dir: dir });
  } catch (e) {
    A.extracting = false;
    $('btnExtractOne').disabled = false;
    $('btnExtractAll').disabled = false;
    throw e;
  }
}

function onExtractProgress(d) {
  if (d.finished) {
    A.extracting = false;
    $('btnExtractOne').disabled = !A.entry;
    $('btnExtractAll').disabled = !A.entries.length;
    const bad = d.failed && d.failed.length
      ? ` · ${d.failed.length} could not be decoded` : '';
    assetHint(`wrote ${d.written} to ${d.out_dir}${bad}`);
    log(`extracted ${d.written} asset${d.written === 1 ? '' : 's'} to ${d.out_dir}`);
    window.gamma.openPath(d.out_dir);
  } else {
    assetHint(`extracting ${d.done + 1} of ${d.total} — ${d.current}`);
  }
}

/* -------------------------------------------------------------------- wire */
$('assetSearch').oninput = onSearch;
$('btnPlay').onclick = () => {
  const a = $('stageAudio');
  a.currentTime = 0;
  a.play().catch(() => {});
};
$('zoomer').onclick = (ev) => {
  const b = ev.target.closest('.chip');
  if (!b) return;
  A.zoom = Number(b.dataset.zoom);
  document.querySelectorAll('#zoomer .chip').forEach((c) => {
    c.classList.toggle('is-on', c === b);
  });
  applyZoom();
};
$('btnExtractOne').onclick = () => { if (A.entry) extract([A.entry]).catch(showError); };
$('btnExtractAll').onclick = () => { if (A.entries.length) extract(A.entries).catch(showError); };

/* the index cost is paid only when someone actually opens the tab */
document.querySelector('nav button[data-pane="assets"]')
  .addEventListener('click', () => assetsInit());
