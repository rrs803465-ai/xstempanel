'use strict';
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const S = { me: null, pub: {}, node: 0, nodes: [], timers: [], cleanup: null, hist: {}, palette: null };
const fmtB = n => { n = +n || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + ' ' + u[i]; };
const fmtT = s => { s = +s || 0; const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60 | 0}s`; };
const pct = (a, b) => b ? Math.min(100, Math.round(a / b * 100)) : 0;
const bank = p => `<div class="bank ${p > 90 ? 'crit' : p > 75 ? 'hot' : ''}"><i style="--w:${p}%"></i></div>`;
const when = t => new Date(t * 1000).toLocaleString();

function toast(msg, bad) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  $('#toasts').append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3000);
}

async function api(path, o = {}) {
  const r = await fetch(path, { method: o.method || 'GET', credentials: 'same-origin', headers: o.body !== undefined ? { 'Content-Type': 'application/json' } : {}, body: o.body !== undefined ? JSON.stringify(o.body) : undefined });
  let d = null;
  try { d = await r.json(); } catch (e) { }
  if (!r.ok) {
    if (r.status === 401 && S.me) { S.me = null; boot(); }
    const m = d && d.detail;
    throw new Error(typeof m === 'string' ? m : m ? JSON.stringify(m) : r.statusText);
  }
  return d;
}
const G = (p, o) => api('/api' + p, o);
const N = (p, o) => api((S.node ? `/api/n/${S.node}` : '/api') + p, o);
const fail = e => toast(e.message || String(e), true);

function modal(html) {
  closeModal();
  const m = document.createElement('div');
  m.className = 'modal';
  m.id = 'modal';
  m.innerHTML = `<div class="card">${html}</div>`;
  m.addEventListener('mousedown', e => { if (e.target === m) closeModal(); });
  document.body.append(m);
  const f = m.querySelector('input,select'); if (f) f.focus();
  return m;
}
const closeModal = () => { const m = $('#modal'); if (m) m.remove(); };

function applyTheme() {
  const p = S.pub;
  document.documentElement.dataset.mode = p.mode || 'dark';
  document.title = p.brand || 'xstem';
  document.documentElement.style.setProperty('--accent', p.accent);
  document.documentElement.style.setProperty('--dim', p.dim);
  const bg = $('#bg');
  bg.innerHTML = '';
  bg.style.backgroundImage = '';
  if (p.bg_type === 'video' && p.bg_url) { const v = document.createElement('video'); v.src = p.bg_url; v.autoplay = v.muted = v.loop = v.playsInline = true; bg.append(v); v.play().catch(() => { }); }
  else if (p.bg_type === 'image' && p.bg_url) bg.style.backgroundImage = `url("${p.bg_url}")`;
}

// ------------------------------------------------------------------ boot, setup, login
async function boot() {
  clearTimers();
  S.pub = await fetch('/api/public').then(r => r.json());
  applyTheme();
  if (S.pub.setup.state !== 'done') return setupScreen();
  try { S.me = await G('/me'); } catch (e) { return loginScreen(); }
  if (S.me.root && S.me.who !== 'cluster') { try { S.nodes = await G('/nodes?stats=0'); } catch (e) { S.nodes = []; } }
  shell();
  route();
}

function setupScreen() {
  $('#app').innerHTML = `<div class="center"><div class="card" style="width:min(640px,100%)"><h1>Installing dependencies</h1>
  <p class="mute" id="sstep">Starting</p>${bank(0)}<div class="log" id="slog" style="margin-top:14px"></div><div class="row" style="margin-top:12px"><button id="sretry" hidden>Retry</button></div></div></div>`;
  $('#sretry').onclick = () => api('/api/setup/retry', { method: 'POST' });
  const tick = async () => {
    const s = await fetch('/api/setup/status').then(r => r.json());
    $('#sstep').textContent = s.state === 'error' ? 'Failed: ' + s.error : s.step + ' (this takes a few minutes the first time)';
    $('#app .bank').outerHTML = bank(s.pct);
    const l = $('#slog'); const stick = l.scrollTop + l.clientHeight >= l.scrollHeight - 20;
    l.textContent = s.log.join('\n'); if (stick) l.scrollTop = l.scrollHeight;
    $('#sretry').hidden = s.state !== 'error';
    if (s.state === 'done') return location.reload();
    S.timers.push(setTimeout(tick, 1500));
  };
  tick().catch(() => S.timers.push(setTimeout(boot, 3000)));
}

function loginScreen() {
  $('#app').innerHTML = `<div class="center"><div class="card"><h1>${esc(S.pub.brand)}</h1><p class="mute">Sign in to manage your machines.</p>
  <label>Realm<select id="realm"><option value="panel">Panel user</option><option value="pam">Linux PAM (root)</option></select></label>
  <label>Username<input id="user" autocomplete="username" autofocus></label>
  <label>Password<input id="pass" type="password" autocomplete="current-password"></label>
  <button class="pri" id="go" style="width:100%">Sign in</button></div></div>`;
  const realm = $('#realm'), user = $('#user');
  realm.onchange = () => { const pam = realm.value === 'pam'; user.value = pam ? 'root' : ''; user.readOnly = pam; (pam ? $('#pass') : user).focus(); };
  const go = async () => {
    try { await G('/login', { method: 'POST', body: { realm: realm.value, username: user.value.trim(), password: $('#pass').value } }); boot(); } catch (e) { fail(e); }
  };
  $('#go').onclick = go;
  $('#pass').onkeydown = e => { if (e.key === 'Enter') go(); };
}

// ------------------------------------------------------------------ shell and routing
function shell() {
  const m = S.me, links = [['overview', 'Overview'], ['machines', 'Machines']];
  if (m.admin) links.push(['images', 'Images'], ['journal', 'Journal']);
  if (m.root) links.push(['nodes', 'Nodes'], ['users', 'Users'], ['look', 'Appearance']);
  links.push(['keys', 'API keys']);
  const multi = m.root && S.nodes.length;
  $('#app').innerHTML = `<div class="app"><nav><div class="brand">${esc(S.pub.brand)}<span>.</span></div>
  ${links.map(([h, t]) => `<a href="#/${h}" data-h="${h}">${t}</a>`).join('')}<a href="/docs" target="_blank">API docs</a>
  <div class="foot">${esc(m.who)}<br>v${esc(S.pub.version)}</div></nav>
  <main><div class="top">${multi ? `<select id="nodesel"><option value="0">This server</option>${S.nodes.filter(n => n.id).map(n => `<option value="${n.id}" ${S.node === n.id ? 'selected' : ''}>${esc(n.name)}</option>`).join('')}</select>` : ''}
  <span class="sp"></span><button id="pal">Search  Ctrl+K</button><button id="out">Sign out</button></div><div id="view"></div></main></div>`;
  if (multi) $('#nodesel').onchange = e => { S.node = +e.target.value; S.hist = {}; route(); };
  $('#pal').onclick = openPalette;
  $('#out').onclick = async () => { await G('/logout', { method: 'POST' }); S.me = null; boot(); };
}

function clearTimers() { S.timers.forEach(t => { clearInterval(t); clearTimeout(t); }); S.timers = []; if (S.cleanup) { S.cleanup(); S.cleanup = null; } }
function live(fn, ms) { const run = () => fn().catch(e => { if (!/installing/.test(e.message)) console.warn(e); }); run(); S.timers.push(setInterval(run, ms)); }

const VIEWS = { overview: vOverview, machines: vMachines, images: vImages, journal: vJournal, nodes: vNodes, users: vUsers, look: vLook, keys: vKeys };
function route() {
  if (!S.me || !$('#view')) return;
  clearTimers();
  const [, name = 'overview', arg] = location.hash.split('/');
  document.querySelectorAll('nav a[data-h]').forEach(a => a.classList.toggle('on', a.dataset.h === (name === 'm' ? 'machines' : name)));
  const v = $('#view');
  v.onclick = null;
  const fn = name === 'm' ? vMachine : VIEWS[name];
  (fn || vOverview)(v, arg).catch(fail);
}
addEventListener('hashchange', route);

// ------------------------------------------------------------------ shared pieces
function spark(c, data, max, color) {
  if (!c) return;
  const w = c.width = c.clientWidth * devicePixelRatio, h = c.height = c.clientHeight * devicePixelRatio, x = c.getContext('2d');
  x.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const m = max || Math.max(1, ...data), step = w / 59;
  x.strokeStyle = color || getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  x.lineWidth = 2 * devicePixelRatio;
  x.beginPath();
  data.forEach((d, i) => { const px = w - (data.length - 1 - i) * step, py = h - 3 - (h - 6) * Math.min(1, d / m); i ? x.lineTo(px, py) : x.moveTo(px, py); });
  x.stroke();
}

function pushHist(g) {
  const s = g.stats, h = S.hist[g.id] || (S.hist[g.id] = { cpu: [], mem: [], rx: [], tx: [], last: null }), now = Date.now();
  if (s && s.running) {
    h.cpu.push(s.cpu); h.mem.push(pct(s.mem_used, s.mem_max));
    const l = h.last, dt = l ? (now - l.t) / 1000 : 0;
    h.rx.push(l && dt > 0 ? Math.max(0, (s.net_in - l.i) / dt) : 0); h.tx.push(l && dt > 0 ? Math.max(0, (s.net_out - l.o) / dt) : 0);
    h.last = { t: now, i: s.net_in, o: s.net_out };
    for (const k of ['cpu', 'mem', 'rx', 'tx']) if (h[k].length > 60) h[k].shift();
  } else { h.last = null; }
  return h;
}

function guestCard(g) {
  const s = g.stats || {};
  return `<a class="card rail ${g.status}" href="#/m/${g.id}" style="color:inherit"><div class="row"><b>${esc(g.name)}</b><span class="tag">${g.kind === 'vm' ? 'VM' : 'CT'}</span><span class="sp"></span><span class="st ${g.status}">${g.status}</span></div>
  <div class="mute mono">${esc(g.ip)}</div><canvas class="spark" data-sp="${g.id}"></canvas>
  <div class="row mute"><span>cpu ${s.cpu || 0}%</span><span class="sp"></span><span>${fmtB(s.mem_used)} / ${fmtB(s.mem_max)}</span></div></a>`;
}

function drawSparks(root) { root.querySelectorAll('canvas[data-sp]').forEach(c => spark(c, (S.hist[c.dataset.sp] || { cpu: [] }).cpu, 100)); }

async function power(id, action) {
  try { await N(`/guests/${id}/power`, { method: 'POST', body: { action } }); toast(action + ' sent'); } catch (e) { fail(e); }
  setTimeout(route, 700);
}

function hostOf() {
  const n = S.nodes.find(n => n.id === S.node);
  try { return n && n.id ? new URL(n.url).hostname : location.hostname; } catch (e) { return location.hostname; }
}

// ------------------------------------------------------------------ overview
function nodeCards(n) {
  const mp = pct(n.mem_used, n.mem_total), dp = pct(n.disk_used, n.disk_total);
  const t = (k, v, extra) => `<div class="card stat"><span class="mute">${k}</span><b>${v}</b>${extra || ''}</div>`;
  return `<div class="grid g3">${t('CPU', Math.round(n.cpu) + '%', bank(Math.round(n.cpu)))}${t('Memory', `${fmtB(n.mem_used)} / ${fmtB(n.mem_total)}`, bank(mp))}
  ${t('Disk', `${fmtB(n.disk_used)} / ${fmtB(n.disk_total)}`, bank(dp))}${t('Load', n.load.map(l => l.toFixed(2)).join('  '), `<span class="mute">${n.cpus} cores, up ${fmtT(n.uptime)}</span>`)}
  ${t('Machines', `${n.running} running`, `<span class="mute">${n.vms} VM, ${n.cts} CT${n.kvm ? '' : ', no KVM (slow emulation)'}</span>`)}${t(esc(n.hostname), esc(n.os), `<span class="mute">kernel ${esc(n.kernel)}</span>`)}</div>`;
}

async function vOverview(v) {
  v.innerHTML = `<h1>Overview</h1><div id="ns" style="margin:16px 0"></div><h2>Machines</h2><div id="gl" class="grid g3" style="margin-top:10px"></div>`;
  live(async () => {
    if (S.me.admin) $('#ns').innerHTML = nodeCards(await N('/node/stats'));
    const gs = await N('/guests?stats=1');
    gs.forEach(pushHist);
    $('#gl').innerHTML = gs.length ? gs.map(guestCard).join('') : `<div class="empty">${S.me.admin ? 'No machines yet. Open Machines and create one.' : 'No machines are assigned to you yet.'}</div>`;
    drawSparks($('#gl'));
  }, 3000);
}

// ------------------------------------------------------------------ machines
async function vMachines(v) {
  v.innerHTML = `<div class="row"><h1>Machines</h1><span class="sp"></span>${S.me.admin ? '<button class="pri" id="new">Create machine</button>' : ''}</div><div id="tb" style="margin-top:14px"></div>`;
  if (S.me.admin) $('#new').onclick = openCreate;
  v.onclick = e => { const b = e.target.closest('button[data-a]'); if (b) { e.stopPropagation(); power(b.dataset.id, b.dataset.a); return; } const r = e.target.closest('tr[data-go]'); if (r) location.hash = '#/m/' + r.dataset.go; };
  live(async () => {
    const gs = await N('/guests?stats=1');
    $('#tb').innerHTML = gs.length ? `<table><tr><th>Name</th><th>Type</th><th>Status</th><th>Address</th><th>Resources</th><th>CPU</th><th></th></tr>${gs.map(g => {
      const b = (a, t) => `<button data-id="${g.id}" data-a="${a}">${t}</button>`;
      return `<tr class="click" data-go="${g.id}"><td><b>${esc(g.name)}</b>${g.owner ? `<br><span class="mute">${esc(g.owner)}</span>` : ''}</td><td>${g.kind.toUpperCase()}</td><td><span class="st ${g.status}">${g.status}</span></td>
      <td class="mono">${esc(g.ip)}</td><td>${g.cores} vCPU, ${g.ram} MB${', ' + g.disk + ' GB'}</td><td style="width:110px">${bank(Math.round(g.stats.cpu))}</td>
      <td class="row">${g.status === 'running' ? b('shutdown', 'Shut down') + b('reboot', 'Reboot') : g.status === 'stopped' ? b('start', 'Start') : ''}</td></tr>`;
    }).join('')}</table>` : `<div class="empty">${S.me.admin ? 'No machines yet.' : 'No machines are assigned to you yet.'}</div>`;
  }, 3000);
}

async function openCreate() {
  let imgs, users = [];
  try { imgs = await N('/images'); } catch (e) { return fail(e); }
  if (S.me.root) { try { users = await G('/users'); } catch (e) { } }
  const ready = imgs.images.filter(i => i.state === 'ready');
  const m = modal(`<h2>Create machine</h2><div style="height:12px"></div>
  <div class="grid g2"><label>Type<select id="k"><option value="vm">Virtual machine (QEMU)</option><option value="ct">Container (LXC)</option></select></label>
  <label>Name<input id="n" placeholder="web-01"></label></div>
  <label id="vi">Image<select id="vim">${ready.map(i => `<option value="${esc(i.key)}">${esc(i.name)}${i.kind === 'iso' ? ' (ISO)' : ''}</option>`).join('') || '<option value="">Download an image first</option>'}</select></label>
  <label id="ci" hidden>Template<input id="cim" list="tpl" value="debian/bookworm"><datalist id="tpl">${imgs.templates.map(t => `<option value="${esc(t)}">`).join('')}</datalist></label>
  <div class="grid g2"><label>vCPUs<input id="c" type="number" min="1" value="1"></label><label>Memory (MB)<input id="r" type="number" min="128" step="128" value="1024"></label>
  <label id="di">Disk (GB)<input id="d" type="number" min="1" value="10"></label><label>Root password<input id="p" placeholder="empty = generate"></label></div>
  <label>Owner<select id="o"><option value="">Nobody (admin only)</option>${users.map(u => `<option>${esc(u.name)}</option>`).join('')}</select></label>
  <label style="display:flex;gap:8px;align-items:center"><input id="a" type="checkbox"> Start when the server boots</label>
  <div class="row"><span class="sp"></span><button id="x">Cancel</button><button class="pri" id="ok">Create</button></div>`);
  const k = $('#k', m);
  k.onchange = () => { const vm = k.value === 'vm'; $('#vi', m).hidden = !vm; $('#ci', m).hidden = vm; };
  $('#x', m).onclick = closeModal;
  $('#ok', m).onclick = async () => {
    const vm = k.value === 'vm';
    try {
      const r = await N('/guests', { method: 'POST', body: { kind: k.value, name: $('#n', m).value.trim(), image: vm ? $('#vim', m).value : $('#cim', m).value.trim(), cores: +$('#c', m).value, ram: +$('#r', m).value, disk: +$('#d', m).value, password: $('#p', m).value, owner: $('#o', m).value, autostart: $('#a', m).checked } });
      modal(`<h2>${esc(r.name)} is being built</h2><p class="mute">Root password, shown once:</p><pre class="code" id="pw">${esc(r.password)}</pre><div class="row"><span class="sp"></span><button class="pri" id="done">Done</button></div>`);
      $('#done').onclick = () => { closeModal(); route(); };
    } catch (e) { fail(e); }
  };
}

async function vMachine(v, id) {
  let g = await N(`/guests/${id}`), term = null;
  v.innerHTML = `<div id="hd"></div><div class="grid g2" style="margin:16px 0" id="tiles"></div><div class="grid g2"><div class="card" id="info"></div><div class="card" id="fw"></div></div>
  <div class="card" style="margin-top:16px"><div class="row"><h2>Console</h2><span class="sp"></span><button id="cb">Connect</button></div><div class="term" style="margin-top:10px"><div id="term"></div></div></div>`;
  const header = () => {
    const b = (a, t, c) => `<button class="${c || ''}" data-a="${a}">${t}</button>`;
    $('#hd').innerHTML = `<div class="row"><h1>${esc(g.name)}</h1><span class="tag">${g.kind === 'vm' ? 'VM' : 'CT'}</span><span class="st ${g.status}">${g.status}</span><span class="sp"></span>
    ${g.status === 'stopped' ? b('start', 'Start', 'pri') : ''}${g.status === 'running' ? b('shutdown', 'Shut down') + b('reboot', 'Reboot') + b('stop', 'Force stop', 'bad') : ''}${S.me.admin ? b('delete', 'Delete', 'bad') : ''}</div>
    ${g.error ? `<p class="bad" style="color:var(--bad)">${esc(g.error)}</p>` : ''}`;
  };
  const tile = (k, val, key, max, unit) => `<div class="card stat"><span class="mute">${k}</span><b>${val}</b><canvas class="spark" data-t="${key}"></canvas></div>`;
  const draw = () => {
    const s = g.stats || {}, h = S.hist[g.id] || { cpu: [], mem: [], rx: [], tx: [] };
    const rx = h.rx.length ? h.rx[h.rx.length - 1] : 0, tx = h.tx.length ? h.tx[h.tx.length - 1] : 0;
    $('#tiles').innerHTML = tile('CPU', (s.cpu || 0) + '%', 'cpu') + tile('Memory', `${fmtB(s.mem_used)} of ${fmtB(s.mem_max)}`, 'mem') +
      tile('Network in / out', `${fmtB(rx)}/s  /  ${fmtB(tx)}/s`, 'rx') + `<div class="card stat"><span class="mute">Disk</span><b>${fmtB(s.disk_used)} of ${fmtB(s.disk_max)}</b>${bank(pct(s.disk_used, s.disk_max))}<span class="mute">up ${fmtT(s.uptime)}</span></div>`;
    spark($('[data-t=cpu]'), h.cpu, 100); spark($('[data-t=mem]'), h.mem, 100); spark($('[data-t=rx]'), h.rx.map((x, i) => x + h.tx[i]));
    const ssh = g.forwards.find(f => f.guest === 22), cmd = ssh ? `ssh root@${hostOf()} -p ${ssh.host}` : '';
    $('#info').innerHTML = `<h2>Details</h2><table><tr><td class="mute">Address</td><td class="mono">${esc(g.ip)}</td></tr><tr><td class="mute">Image</td><td>${esc(g.image)}</td></tr>
    <tr><td class="mute">Resources</td><td>${g.cores} vCPU, ${g.ram} MB${', ' + g.disk + ' GB'}</td></tr><tr><td class="mute">Owner</td><td>${esc(g.owner) || '-'}</td></tr>
    ${S.me.admin ? `<tr><td class="mute">Autostart</td><td><input type="checkbox" id="auto" ${g.autostart ? 'checked' : ''}></td></tr>` : ''}
    ${g.iso ? `<tr><td class="mute">Install ISO</td><td>attached <button data-a="eject">Eject</button></td></tr>` : ''}</table>
    ${cmd ? `<p class="mute" style="margin-bottom:4px">SSH</p><div class="row"><code class="mono sp">${esc(cmd)}</code><button data-copy="${esc(cmd)}">Copy</button></div>` : ''}`;
    const fwIn = document.activeElement && document.activeElement.id === 'fp';
    if (!fwIn) $('#fw').innerHTML = `<h2>Port forwards</h2><table><tr><th>Server port</th><th>Machine port</th><th></th></tr>${g.forwards.map(f => `<tr><td class="mono">${f.host}</td><td class="mono">${f.guest}</td><td>${f.guest === 22 ? '' : `<button class="bad" data-fd="${f.host}">Remove</button>`}</td></tr>`).join('')}</table>
    <div class="row" style="margin-top:10px"><input id="fp" type="number" min="1" max="65535" placeholder="machine port" style="width:150px"><button id="fa">Forward</button></div>`;
    const a = $('#auto'); if (a) a.onchange = () => N(`/guests/${g.id}`, { method: 'PATCH', body: { autostart: a.checked } }).catch(fail);
    $('#cb').disabled = g.status !== 'running';
  };
  header(); draw();
  v.onclick = async e => {
    const t = e.target.closest('[data-a],[data-copy],[data-fd],#fa,#cb'); if (!t) return;
    try {
      if (t.dataset.copy) { await navigator.clipboard.writeText(t.dataset.copy); return toast('copied'); }
      if (t.dataset.fd) { g = await N(`/guests/${g.id}/forwards/${t.dataset.fd}`, { method: 'DELETE' }); return refresh(); }
      if (t.id === 'fa') { const p = +$('#fp').value; if (!p) return; await N(`/guests/${g.id}/forwards`, { method: 'POST', body: { guest_port: p } }); return refresh(); }
      if (t.id === 'cb') { if (term) { term(); term = null; t.textContent = 'Connect'; } else { term = openConsole(g); t.textContent = 'Disconnect'; } return; }
      const a = t.dataset.a;
      if (a === 'eject') { await N(`/guests/${g.id}/eject`, { method: 'POST' }); toast('ISO ejected, takes effect on next start'); return refresh(); }
      if (a === 'delete') { if (!confirm(`Delete ${g.name} and all of its data?`)) return; await N(`/guests/${g.id}`, { method: 'DELETE' }); toast('deleted'); location.hash = '#/machines'; return; }
      await N(`/guests/${g.id}/power`, { method: 'POST', body: { action: a } }); toast(a + ' sent'); setTimeout(refresh, 800);
    } catch (err) { fail(err); }
  };
  const refresh = async () => { g = await N(`/guests/${id}`); pushHist(g); header(); draw(); };
  S.cleanup = () => { if (term) term(); };
  live(refresh, 3000);
}

function openConsole(g) {
  const el = $('#term');
  if (!window.Terminal) { el.textContent = 'Console assets are not installed yet. Reload in a minute.'; return () => { }; }
  el.textContent = '';
  const t = new Terminal({ cursorBlink: true, fontSize: 14, fontFamily: 'ui-monospace,Menlo,Consolas,monospace', theme: { background: '#05080f' } });
  const fit = new FitAddon.FitAddon();
  t.loadAddon(fit); t.open(el); fit.fit();
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${S.node ? '/ws/n/' + S.node : '/ws'}/console/${g.id}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { ws.send(JSON.stringify({ resize: [t.cols, t.rows] })); t.focus(); };
  ws.onmessage = e => t.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data));
  ws.onclose = () => t.write('\r\n\x1b[33m[disconnected]\x1b[0m\r\n');
  t.onData(d => { if (ws.readyState === 1) ws.send(d); });
  t.onResize(s => { if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [s.cols, s.rows] })); });
  const rs = () => fit.fit(); addEventListener('resize', rs);
  return () => { removeEventListener('resize', rs); ws.close(); t.dispose(); };
}

// ------------------------------------------------------------------ images
async function vImages(v) {
  v.innerHTML = `<h1>Images</h1><p class="mute">Cloud images boot ready to use with a root password. ISOs attach as an install disc.</p><div id="il" style="margin:14px 0"></div>
  <div class="card"><h2>Add from URL</h2><div class="grid g2" style="margin-top:10px"><label>Name<input id="in"></label><label>Type<select id="ik"><option value="cloud">Cloud image (qcow2 / img)</option><option value="iso">ISO</option></select></label></div>
  <label>URL<input id="iu" placeholder="https://"></label><button class="pri" id="ia">Download</button></div>`;
  $('#ia').onclick = async () => { try { await N('/images', { method: 'POST', body: { name: $('#in').value, url: $('#iu').value.trim(), kind: $('#ik').value } }); toast('download started'); route(); } catch (e) { fail(e); } };
  v.onclick = async e => {
    const b = e.target.closest('button[data-i]'); if (!b) return;
    try { if (b.dataset.k === 'dl') await N(`/images/${b.dataset.i}/download`, { method: 'POST' }); else if (confirm('Delete this image file?')) await N(`/images/${b.dataset.i}`, { method: 'DELETE' }); } catch (err) { fail(err); }
    refresh();
  };
  const refresh = async () => {
    const r = await N('/images');
    $('#il').innerHTML = `<table><tr><th>Name</th><th>Type</th><th>Status</th><th>Size</th><th></th></tr>${r.images.map(i => `<tr><td>${esc(i.name)}</td><td>${i.kind}</td>
    <td><span class="st ${i.state === 'ready' ? 'running' : i.state === 'error' ? 'error' : i.state}">${i.state === 'downloading' ? pct(i.got, i.size) + '%' : i.state === 'new' ? 'not downloaded' : i.state}</span>${i.error ? `<br><span class="mute">${esc(i.error)}</span>` : ''}</td>
    <td>${i.state === 'ready' ? fmtB(i.size) : i.state === 'downloading' ? `${fmtB(i.got)} / ${fmtB(i.size)}` : ''}</td>
    <td>${i.state === 'new' || i.state === 'error' ? `<button class="pri" data-k="dl" data-i="${i.id}">Download</button>` : i.state === 'ready' ? `<button class="bad" data-k="rm" data-i="${i.id}">Delete</button>` : ''}</td></tr>`).join('')}</table>`;
  };
  live(refresh, 2500);
}

// ------------------------------------------------------------------ journal
async function vJournal(v) {
  v.innerHTML = `<h1>Journal</h1><div id="jl" style="margin-top:14px"></div>`;
  live(async () => { const ev = await N('/events'); $('#jl').innerHTML = `<table><tr><th>When</th><th>Who</th><th>What</th></tr>${ev.map(e => `<tr><td class="mute">${when(e.ts)}</td><td>${esc(e.who)}</td><td>${esc(e.msg)}</td></tr>`).join('')}</table>`; }, 5000);
}

// ------------------------------------------------------------------ nodes
async function vNodes(v) {
  const tok = (await G('/node/token')).token;
  v.innerHTML = `<h1>Nodes</h1><div id="nl" style="margin:14px 0"></div>
  <div class="grid g2"><div class="card"><h2>Link a node</h2><p class="mute">Install xstem on the other server, then enter its address and its node token here.</p>
  <label>Name<input id="nn" placeholder="node-2"></label><label>Address<input id="nu" placeholder="203.0.113.7:8443"></label><label>Node token<input id="nt"></label><button class="pri" id="na">Link node</button></div>
  <div class="card"><h2>This server's node token</h2><p class="mute">Give this to another panel to let it manage this server. Treat it like the root password.</p>
  <pre class="code" id="tk">${esc(tok)}</pre><div class="row"><button data-copy>Copy</button><button class="bad" id="tn">Generate new token</button></div></div></div>`;
  $('#na').onclick = async () => {
    try { const r = await G('/nodes', { method: 'POST', body: { name: $('#nn').value.trim(), url: $('#nu').value.trim(), token: $('#nt').value.trim() } }); toast('linked, certificate ' + r.fingerprint.slice(0, 16)); S.nodes = await G('/nodes?stats=0'); shell(); route(); } catch (e) { fail(e); }
  };
  $('#tn').onclick = async () => { if (confirm('Panels using the old token will lose access.')) { const r = await G('/node/token', { method: 'POST' }); $('#tk').textContent = r.token; } };
  v.onclick = async e => {
    if (e.target.closest('[data-copy]')) { await navigator.clipboard.writeText($('#tk').textContent); return toast('copied'); }
    const b = e.target.closest('button[data-rm]'); if (!b || !confirm('Unlink this node?')) return;
    try { await G('/nodes/' + b.dataset.rm, { method: 'DELETE' }); S.nodes = await G('/nodes?stats=0'); if (S.node === +b.dataset.rm) S.node = 0; shell(); route(); } catch (err) { fail(err); }
  };
  live(async () => {
    const ns = await G('/nodes');
    $('#nl').innerHTML = `<table><tr><th>Node</th><th>Status</th><th>CPU</th><th>Memory</th><th>Machines</th><th></th></tr>${ns.map(n => { const s = n.stats; return `<tr><td><b>${esc(n.name)}</b><br><span class="mute mono">${esc(n.url)}</span></td>
    <td><span class="st ${n.online ? 'running' : 'error'}">${n.online ? 'online' : 'offline'}</span></td>
    <td style="width:130px">${s ? bank(Math.round(s.cpu)) + `<span class="mute">${Math.round(s.cpu)}% of ${s.cpus} cores</span>` : ''}</td>
    <td style="width:150px">${s ? bank(pct(s.mem_used, s.mem_total)) + `<span class="mute">${fmtB(s.mem_used)} / ${fmtB(s.mem_total)}</span>` : ''}</td>
    <td>${s ? `${s.running} running, ${s.vms + s.cts} total` : ''}</td><td>${n.id ? `<button class="bad" data-rm="${n.id}">Unlink</button>` : ''}</td></tr>`; }).join('')}</table>`;
  }, 5000);
}

// ------------------------------------------------------------------ users and keys
async function vUsers(v) {
  v.innerHTML = `<h1>Users</h1><p class="mute">Panel users sign in with the Panel realm. Assign machines to them when you create a machine.</p><div id="ul" style="margin:14px 0"></div>
  <div class="card"><h2>Create user</h2><div class="grid g3" style="margin-top:10px"><label>Username<input id="un"></label><label>Password (8+)<input id="up" type="password"></label>
  <label>Role<select id="ur"><option value="user">User: own machines only</option><option value="admin">Admin: all machines on this server</option></select></label></div><button class="pri" id="uc">Create user</button></div>`;
  $('#uc').onclick = async () => { try { await G('/users', { method: 'POST', body: { name: $('#un').value.trim(), password: $('#up').value, role: $('#ur').value } }); toast('user created'); route(); } catch (e) { fail(e); } };
  v.onclick = async e => {
    const b = e.target.closest('button[data-u]'); if (!b) return;
    const n = b.dataset.u;
    try {
      if (b.dataset.k === 'pw') { const p = prompt('New password for ' + n + ' (8+ characters)'); if (!p) return; await G('/users/' + n, { method: 'PATCH', body: { password: p } }); toast('password changed'); }
      else if (b.dataset.k === 'dis') await G('/users/' + n, { method: 'PATCH', body: { disabled: b.dataset.v === '1' } });
      else if (confirm('Delete ' + n + '? Their machines become unassigned.')) await G('/users/' + n, { method: 'DELETE' });
    } catch (err) { fail(err); }
    route();
  };
  const us = await G('/users');
  $('#ul').innerHTML = us.length ? `<table><tr><th>User</th><th>Role</th><th>Machines</th><th>Status</th><th></th></tr>${us.map(u => `<tr><td><b>${esc(u.name)}</b></td><td>${u.role}</td><td>${u.machines}</td>
  <td><span class="st ${u.disabled ? 'error' : 'running'}">${u.disabled ? 'disabled' : 'active'}</span></td><td class="row"><button data-k="pw" data-u="${esc(u.name)}">Set password</button>
  <button data-k="dis" data-u="${esc(u.name)}" data-v="${u.disabled ? 0 : 1}">${u.disabled ? 'Enable' : 'Disable'}</button><button class="bad" data-k="rm" data-u="${esc(u.name)}">Delete</button></td></tr>`).join('')}</table>` : '<div class="empty">No panel users yet.</div>';
}

async function vKeys(v) {
  v.innerHTML = `<div class="row"><h1>API keys</h1><span class="sp"></span><a href="/docs" target="_blank">Read the API docs</a></div>
  <p class="mute">A key acts as ${esc(S.me.who)}. Send it as <code class="mono">Authorization: Bearer KEY</code>.</p><div id="kl" style="margin:14px 0"></div>
  <div class="row"><input id="kn" placeholder="Key name, for example discord-bot" style="max-width:320px"><button class="pri" id="kc">Create key</button></div>`;
  $('#kc').onclick = async () => {
    try { const r = await G('/keys', { method: 'POST', body: { name: $('#kn').value || 'key' } }); modal(`<h2>Your new key</h2><p class="mute">It is shown only once. Store it now.</p><pre class="code">${esc(r.key)}</pre><div class="row"><span class="sp"></span><button class="pri" id="kd">Done</button></div>`); $('#kd').onclick = () => { closeModal(); route(); }; } catch (e) { fail(e); }
  };
  v.onclick = async e => { const b = e.target.closest('button[data-k]'); if (b) { await G('/keys/' + b.dataset.k, { method: 'DELETE' }).catch(fail); route(); } };
  const ks = await G('/keys');
  $('#kl').innerHTML = ks.length ? `<table><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th></th></tr>${ks.map(k => `<tr><td>${esc(k.name)}</td><td class="mono">${esc(k.prefix)}...</td><td>${when(k.created)}</td><td>${k.last ? when(k.last) : 'never'}</td><td><button class="bad" data-k="${k.id}">Revoke</button></td></tr>`).join('')}</table>` : '<div class="empty">No keys yet.</div>';
}

// ------------------------------------------------------------------ appearance
async function vLook(v) {
  const s = await G('/settings');
  v.innerHTML = `<h1>Appearance</h1><div class="grid g2" style="margin-top:14px"><div class="card"><label>Panel name<input id="lb" value="${esc(s.brand)}"></label>
  <div class="grid g2"><label>Accent color<input id="la" type="color" value="${esc(s.accent)}"></label><label>Mode<select id="lm"><option value="dark">Dark</option><option value="light">Light</option></select></label></div>
  <label>Background dimming<input id="ld" type="range" min="0" max="1" step="0.05" value="${esc(s.dim)}"></label>
  <label>Custom CSS<textarea id="lc" rows="8" class="mono" spellcheck="false">${esc(s.css)}</textarea></label></div>
  <div class="card"><h2>Background</h2><label>Type<select id="lt"><option value="none">None</option><option value="image">Image</option><option value="video">Video</option></select></label>
  <label>Address<input id="lu" value="${esc(s.bg_url)}" placeholder="https://... or upload below"></label>
  <label>Upload mp4, webm, jpg, png, webp or gif (150 MB max)<input id="lf" type="file" accept="video/mp4,video/webm,image/*"></label><button id="lup">Upload</button></div></div>
  <div class="row" style="margin-top:14px"><button class="pri" id="ls">Save appearance</button></div>`;
  $('#lm').value = s.mode; $('#lt').value = s.bg_type;
  const save = async () => {
    await G('/settings', { method: 'PUT', body: { brand: $('#lb').value, accent: $('#la').value, mode: $('#lm').value, dim: +$('#ld').value, css: $('#lc').value, bg_type: $('#lt').value, bg_url: $('#lu').value.trim() } });
    S.pub = await fetch('/api/public').then(r => r.json()); applyTheme();
    document.getElementById('theme-css')?.remove();
    const l = document.createElement('link'); l.rel = 'stylesheet'; l.id = 'theme-css'; l.href = '/theme.css?' + Date.now(); document.head.append(l);
    shell(); route();
  };
  $('#ls').onclick = () => save().then(() => toast('saved')).catch(fail);
  $('#lup').onclick = async () => {
    const f = $('#lf').files[0]; if (!f) return toast('choose a file first', true);
    try {
      const r = await fetch('/api/settings/background?name=' + encodeURIComponent(f.name), { method: 'PUT', body: f, credentials: 'same-origin' });
      const d = await r.json(); if (!r.ok) throw new Error(d.detail);
      $('#lu').value = d.url; $('#lt').value = d.type; await save(); toast('background set');
    } catch (e) { fail(e); }
  };
}

// ------------------------------------------------------------------ command palette
async function openPalette() {
  if (!S.me || $('#modal')) return;
  let gs = []; try { gs = await N('/guests'); } catch (e) { }
  const items = [['Overview', () => location.hash = '#/overview'], ['Machines', () => location.hash = '#/machines'], ['API keys', () => location.hash = '#/keys']];
  if (S.me.admin) items.push(['Images', () => location.hash = '#/images'], ['Journal', () => location.hash = '#/journal'], ['Create machine', () => { location.hash = '#/machines'; setTimeout(openCreate, 150); }]);
  if (S.me.root) items.push(['Nodes', () => location.hash = '#/nodes'], ['Users', () => location.hash = '#/users'], ['Appearance', () => location.hash = '#/look']);
  gs.forEach(g => {
    items.push([`Open ${g.name}`, () => location.hash = '#/m/' + g.id]);
    if (g.status === 'stopped') items.push([`Start ${g.name}`, () => power(g.id, 'start')]);
    if (g.status === 'running') items.push([`Shut down ${g.name}`, () => power(g.id, 'shutdown')], [`Reboot ${g.name}`, () => power(g.id, 'reboot')]);
  });
  const m = modal(`<input id="pq" placeholder="Type to jump or act"><div id="pr" style="margin-top:8px"></div>`);
  m.classList.add('pal');
  let sel = 0, list = [];
  const draw = () => {
    const w = $('#pq', m).value.toLowerCase().split(/\s+/).filter(Boolean);
    list = items.filter(i => w.every(x => i[0].toLowerCase().includes(x))).slice(0, 9); sel = Math.min(sel, Math.max(0, list.length - 1));
    $('#pr', m).innerHTML = list.map((i, n) => `<div class="it ${n === sel ? 'on' : ''}" data-n="${n}">${esc(i[0])}</div>`).join('') || '<div class="mute" style="padding:7px 10px">Nothing matches</div>';
  };
  const run = n => { const i = list[n]; if (i) { closeModal(); i[1](); } };
  $('#pq', m).oninput = () => { sel = 0; draw(); };
  $('#pq', m).onkeydown = e => { if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, list.length - 1); draw(); e.preventDefault(); } else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); } else if (e.key === 'Enter') run(sel); else if (e.key === 'Escape') closeModal(); };
  $('#pr', m).onclick = e => { const it = e.target.closest('.it'); if (it) run(+it.dataset.n); };
  draw();
}
addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); } else if (e.key === 'Escape') closeModal(); });

boot().catch(e => { $('#app').innerHTML = `<div class="center"><div class="card"><h1>Panel is starting</h1><p class="mute">${esc(e.message)}</p></div></div>`; setTimeout(boot, 3000); });
