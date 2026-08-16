'use strict';
const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const readline = require('readline');

let win = null;
let daemon = null;
let nextId = 1;
const pending = new Map();

/** Frozen binary when packaged, plain Python in development. */
function daemonCommand() {
  const frozen = path.join(process.resourcesPath || '', 'daemon', 'gamma-daemon.exe');
  if (fs.existsSync(frozen)) return { cmd: frozen, args: [], cwd: path.dirname(frozen) };
  const script = path.join(__dirname, '..', 'daemon', 'main.py');
  const py = process.env.GAMMA_PYTHON || 'python';
  return { cmd: py, args: ['-u', script], cwd: path.dirname(script) };
}

function toRenderer(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function startDaemon() {
  const { cmd, args, cwd } = daemonCommand();
  daemon = spawn(cmd, args, { cwd, windowsHide: true });

  readline.createInterface({ input: daemon.stdout }).on('line', (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.event) return toRenderer('daemon-event', msg);
    const slot = pending.get(msg.id);
    if (!slot) return;
    pending.delete(msg.id);
    msg.ok ? slot.resolve(msg.result) : slot.reject(new Error(msg.error || 'daemon error'));
  });

  // stderr is diagnostics only. Surfacing it means a Python traceback shows up in
  // the app instead of vanishing into a console nobody is watching.
  readline.createInterface({ input: daemon.stderr }).on('line', (line) => {
    toRenderer('daemon-event', { event: 'log', data: { line } });
  });

  daemon.on('exit', (code) => {
    for (const [, slot] of pending) slot.reject(new Error('daemon exited'));
    pending.clear();
    toRenderer('daemon-event', { event: 'exit', data: { code } });
  });

  daemon.on('error', (err) => {
    toRenderer('daemon-event', { event: 'log', data: { line: 'daemon failed to start: ' + err.message } });
  });
}

function call(method, params) {
  return new Promise((resolve, reject) => {
    if (!daemon || daemon.exitCode !== null) return reject(new Error('daemon is not running'));
    const id = nextId++;
    pending.set(id, { resolve, reject });
    daemon.stdin.write(JSON.stringify({ id, method, params: params || {} }) + '\n');
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(method + ' timed out')); }
    }, 300000);
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1180, height: 800, minWidth: 940, minHeight: 640,
    backgroundColor: '#151826',
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.once('ready-to-show', () => win.show());
  // dev affordance: --autohunt=<starter> makes the window attach and run a forced
  // hunt on its own, so the UI can be verified without a human driving it
  const auto = (process.argv.find((a) => a.startsWith('--autohunt')) || '').split('=')[1];
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'),
               auto ? { search: 'autohunt=' + auto } : undefined);
  if (process.argv.includes('--dev')) win.webContents.openDevTools({ mode: 'detach' });

  // dev affordance: --shot=<png> clicks through to a tab, saves a screenshot and
  // exits. Reporting "the UI works" without looking at it is how a blank panel
  // ships.
  const shot = (process.argv.find((a) => a.startsWith('--shot=')) || '').split('=')[1];
  if (shot) {
    const tab = (process.argv.find((a) => a.startsWith('--tab=')) || '').split('=')[1];
    const waitMs = Number((process.argv.find((a) => a.startsWith('--wait=')) || '').split('=')[1] || 12000);
    win.webContents.on('console-message', (_e, _lvl, message) => {
      process.stderr.write('[renderer] ' + message + '\n');
    });
    win.webContents.once('did-finish-load', async () => {
      if (tab) {
        await win.webContents.executeJavaScript(
          `document.querySelector('nav button[data-pane="${tab}"]').click(); true`);
      }
      const js = (process.argv.find((a) => a.startsWith('--eval=')) || '').slice(7);
      if (js) await win.webContents.executeJavaScript(js + '; true');
      const find = (process.argv.find((a) => a.startsWith('--find=')) || '').split('=')[1];
      if (find) {
        await new Promise((r) => setTimeout(r, 6000));
        await win.webContents.executeJavaScript(
          `(() => { const s = document.getElementById('assetSearch');
                    s.value = ${JSON.stringify(find)};
                    s.dispatchEvent(new Event('input')); return true; })()`);
      }
      setTimeout(async () => {
        const img = await win.webContents.capturePage();
        fs.writeFileSync(shot, img.toPNG());
        process.stderr.write('[shot] wrote ' + shot + '\n');
        app.quit();
      }, waitMs);
    });
  }
}

app.whenReady().then(() => { startDaemon(); createWindow(); });
app.on('window-all-closed', () => { if (daemon) daemon.kill(); app.quit(); });

/* Settings live in Electron's userData, so the chosen game path survives a
   restart. The built-in specs point at the author's drive; without this the app
   would ask every session on anyone else's machine. */
const settingsFile = () => path.join(app.getPath('userData'), 'settings.json');

function readSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile(), 'utf8')); }
  catch { return {}; }
}

function writeSettings(patch) {
  const next = { ...readSettings(), ...patch };
  try {
    fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
    fs.writeFileSync(settingsFile(), JSON.stringify(next, null, 2));
  } catch (e) {
    toRenderer('daemon-event', { event: 'log', data: { line: 'could not save settings: ' + e.message } });
  }
  return next;
}

ipcMain.handle('settings-get', () => readSettings());
ipcMain.handle('settings-set', (_e, patch) => writeSettings(patch));
ipcMain.handle('pick-exe', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Select the game executable',
    properties: ['openFile'],
    filters: [{ name: 'Game', extensions: ['exe'] }],
    defaultPath: readSettings().gameExe || undefined,
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle('rpc', (_e, method, params) => call(method, params));
ipcMain.handle('pick-folder', async () => {
  const r = await dialog.showOpenDialog(win, { properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle('open-path', (_e, p) => shell.openPath(p));
