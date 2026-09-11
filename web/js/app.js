/**
 * Codeville front end.
 *
 * Holds a mirror of the daemon's world, applies the delta events it streams, and
 * keeps each village's pixel scene in sync. The daemon is the single source of
 * truth: this module never invents state, it only reflects what Claude Code did.
 *
 * Village nodes are created **once and updated in place**. An earlier version
 * rebuilt a village's DOM on every change, which threw away its canvas and with
 * it every actor's position — characters teleported instead of walking. Now the
 * card's text is patched and the scene is handed the new agent list, so actors
 * keep their place in the world and walk to wherever the work moved.
 */

import { avatarSvg, SPECIES, speciesFor, speciesLabel } from './characters.js';
import {
  LOGICAL_H, LOGICAL_W, Scene, backdropFor, loadSprite, registerScene, unregisterScene,
} from './scene.js';

/* --------------------------------------------------------------- state */

const state = {
  villages: new Map(),   // slug -> village (with sessions[])
  agents: new Map(),     // agentId -> {agent, villageSlug}
  stats: {},
};

const els = {};
const nodes = new Map();   // slug -> {root, scene, sign, badge, path, bubble}
const dirty = new Set();
let repaintQueued = false;

/* --------------------------------------------------------------- sprites */

const SPRITE_NAMES = [
  'mayor', 'worker', 'explorer', 'planner', 'reviewer',
  'generalist', 'guide', 'tinkerer', 'villager',
  'station_run', 'station_edit', 'station_read',
  'station_write', 'station_search', 'station_web',
  'scene_meadow', 'scene_forest', 'scene_harbor', 'scene_canyon', 'scene_citadel',
];

function preloadSprites() {
  for (const name of SPRITE_NAMES) loadSprite(name, `assets/sprites/${name}.png`);
}

/* ---------------------------------------------------------- connection */

function tokenFromLocation() {
  const fromQuery = new URLSearchParams(location.search).get('token');
  if (fromQuery) {
    try {
      sessionStorage.setItem('codeville.token', fromQuery);
      const keep = new URLSearchParams(location.search);
      keep.delete('token');
      const suffix = keep.toString();
      history.replaceState(null, '', location.pathname + (suffix ? '?' + suffix : ''));
    } catch (_) { /* private mode: carry on with the in-memory value */ }
    return fromQuery;
  }
  try { return sessionStorage.getItem('codeville.token') || ''; } catch (_) { return ''; }
}

const PARAMS = new URLSearchParams(location.search);
const THEME_OVERRIDE = PARAMS.get('theme');
const SHOW_ALL_OVERRIDE = ['1', 'true', 'yes'].includes((PARAMS.get('all') || '').toLowerCase());
const TOKEN = tokenFromLocation();

let socket = null;
let reconnectDelay = 500;

function connect() {
  if (!TOKEN) { setConnection('down', 'no token'); showTokenHelp(); return; }
  setConnection('wait', 'connecting');
  try {
    socket = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  } catch (_) { scheduleReconnect(); return; }

  socket.addEventListener('open', () => {
    reconnectDelay = 500;
    setConnection('live', 'watching');
  });
  socket.addEventListener('message', (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }
    handleEvent(payload);
  });
  socket.addEventListener('close', () => { setConnection('down', 'reconnecting'); scheduleReconnect(); });
  socket.addEventListener('error', () => { try { socket.close(); } catch (_) {} });
}

function scheduleReconnect() {
  setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.8, 10000);
}

function setConnection(kind, label) {
  if (!els.conn) return;
  els.conn.className = `cv-conn cv-conn--${kind === 'live' ? 'live' : kind === 'down' ? 'down' : 'wait'}`;
  els.conn.querySelector('.cv-conn__text').textContent = label;
}

/* -------------------------------------------------------------- events */

function handleEvent(event) {
  switch (event.t) {
    case 'hello': loadSnapshot(event); break;
    case 'village.upsert': upsertVillage(event.village); break;
    case 'session.update':
      withSession(event.village, event.session, (s) => { if (event.title) s.title = event.title; });
      break;
    case 'session.end': removeSession(event.village, event.session); break;
    case 'agent.spawn':
    case 'agent.update': upsertAgent(event.agent); break;
    case 'agent.state':
      patchAgent(event.agent, (a) => { a.state = event.state; if (event.state !== 'working') a.tool = null; });
      break;
    case 'agent.tool':
      patchAgent(event.agent, (a) => { a.tool = event.tool; a.state = 'working'; });
      break;
    case 'agent.tool_end':
      patchAgent(event.agent, (a) => { a.lastOk = event.ok; });
      break;
    case 'agent.say': patchAgent(event.agent, (a) => { a.say = event.text; }); break;
    case 'agent.done':
      patchAgent(event.agent, (a) => { a.state = event.ok ? 'done' : 'failed'; a.tool = null; });
      break;
    case 'agent.despawn': removeAgent(event.agent); break;
    case 'workflow.start':
    case 'workflow.update':
      withSession(event.village, event.session, (s) => {
        s.workflows = (s.workflows || []).filter((w) => w.run_id !== event.workflow.run_id);
        s.workflows.push(event.workflow);
      });
      break;
    default: return;
  }
  recomputeStats();
  queueRepaint();
}

function loadSnapshot(snapshot) {
  state.villages.clear();
  state.agents.clear();
  for (const village of snapshot.villages || []) upsertVillage(village, true);
  state.stats = snapshot.stats || {};
  dirty.add('*');
  queueRepaint();
}

function upsertVillage(incoming, withSessions = false) {
  const existing = state.villages.get(incoming.slug);
  if (!existing) {
    const village = { ...incoming, sessions: incoming.sessions || [] };
    state.villages.set(village.slug, village);
    indexVillageAgents(village);
    dirty.add('*');
    return;
  }
  const sessions = withSessions && incoming.sessions ? incoming.sessions : existing.sessions;
  Object.assign(existing, incoming, { sessions });
  if (withSessions && incoming.sessions) indexVillageAgents(existing);
  dirty.add(existing.slug);
}

function indexVillageAgents(village) {
  for (const session of village.sessions || []) {
    for (const agent of session.villagers || []) {
      state.agents.set(agent.id, { agent, villageSlug: village.slug });
    }
  }
}

function withSession(slug, sessionId, fn) {
  const village = state.villages.get(slug);
  if (!village) return;
  let session = (village.sessions || []).find((s) => s.id === sessionId);
  if (!session) {
    session = { id: sessionId, village: slug, villagers: [], workflows: [] };
    village.sessions = village.sessions || [];
    village.sessions.push(session);
  }
  fn(session);
  dirty.add(slug);
}

function upsertAgent(agent) {
  if (!agent || !agent.id) return;
  const known = state.agents.get(agent.id);
  if (known) {
    Object.assign(known.agent, agent);
    dirty.add(known.villageSlug);
    return;
  }
  const slug = findVillageForSession(agent.session);
  if (!slug) return;
  withSession(slug, agent.session, (session) => {
    session.villagers = session.villagers || [];
    session.villagers.push(agent);
  });
  state.agents.set(agent.id, { agent, villageSlug: slug });
  dirty.add(slug);
}

function findVillageForSession(sessionId) {
  for (const village of state.villages.values()) {
    if ((village.sessions || []).some((s) => s.id === sessionId)) return village.slug;
  }
  const newest = [...state.villages.values()]
    .sort((a, b) => (b.last_active || 0) - (a.last_active || 0))[0];
  return newest ? newest.slug : null;
}

function patchAgent(agentId, fn) {
  const known = state.agents.get(agentId);
  if (!known) return;
  fn(known.agent);
  dirty.add(known.villageSlug);
}

function removeAgent(agentId) {
  const known = state.agents.get(agentId);
  if (!known) return;
  const village = state.villages.get(known.villageSlug);
  if (village) {
    for (const session of village.sessions || []) {
      session.villagers = (session.villagers || []).filter((v) => v.id !== agentId);
    }
  }
  state.agents.delete(agentId);
  dirty.add(known.villageSlug);
}

function removeSession(slug, sessionId) {
  const village = state.villages.get(slug);
  if (!village) return;
  const session = (village.sessions || []).find((s) => s.id === sessionId);
  for (const agent of (session && session.villagers) || []) state.agents.delete(agent.id);
  village.sessions = (village.sessions || []).filter((s) => s.id !== sessionId);
  dirty.add(slug);
}

function recomputeStats() {
  let working = 0;
  let live = 0;
  for (const village of state.villages.values()) {
    const agents = agentsOf(village);
    if (agents.some((a) => !isFinished(a) && a.state !== 'idle')) live += 1;
    working += agents.filter((a) => a.state === 'working').length;
  }
  state.stats = {
    ...state.stats,
    villages: state.villages.size,
    villages_live: live,
    agents_working: working,
    agents: state.agents.size,
  };
}

const isFinished = (a) => a.state === 'done' || a.state === 'failed';
const agentsOf = (village) => (village.sessions || []).flatMap((s) => s.villagers || []);

const STATE_ORDER = { working: 0, thinking: 1, spawning: 2, failed: 3, done: 4, idle: 5 };

/**
 * Which villagers earn a place on the green.
 *
 * A village accumulates one mayor per session, so an old repo ends up with a row
 * of identical dozing mayors that say nothing. Show everyone doing something,
 * plus the current session's mayor even when it is waiting on you.
 */
function visibleAgents(village) {
  const sessions = [...(village.sessions || [])]
    .sort((a, b) => (b.last_activity || 0) - (a.last_activity || 0));
  const keep = new Map();
  for (const session of sessions) {
    for (const agent of session.villagers || []) {
      if (agent.state !== 'idle') keep.set(agent.id, agent);
    }
  }
  for (const agent of (sessions[0] && sessions[0].villagers) || []) {
    if (agent.agent_type === 'mayor') keep.set(agent.id, agent);
  }
  return [...keep.values()]
    .sort((a, b) => (STATE_ORDER[a.state] ?? 9) - (STATE_ORDER[b.state] ?? 9))
    .slice(0, 16)
    .map((a) => ({ ...a, species: speciesFor(a.agent_type) }));
}

/* ------------------------------------------------------------ rendering */

function queueRepaint() {
  if (repaintQueued) return;
  repaintQueued = true;
  requestAnimationFrame(() => { repaintQueued = false; paint(); });
}

function paint() {
  paintStats();
  const full = dirty.has('*');
  const slugs = full ? [...state.villages.keys()] : [...dirty];
  if (full) reflowWorld();
  for (const slug of slugs) updateVillage(slug);
  dirty.clear();
}

function paintStats() {
  if (!els.stats) return;
  const s = state.stats || {};
  const items = [
    [s.villages || 0, 'villages', false],
    [s.villages_live || 0, 'busy', true],
    [s.agents_working || 0, 'working', true],
    [s.tool_calls || 0, 'tool calls', false],
  ];
  els.stats.innerHTML = items.map(([value, label, live]) => `
    <div class="cv-stat ${live ? 'cv-stat--live' : ''}">
      <span class="cv-stat__value">${formatNumber(value)}</span>
      <span class="cv-stat__label">${label}</span>
    </div>`).join('');
}

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function sortedVillages() {
  const showAll = els.toggleAll && els.toggleAll.getAttribute('aria-pressed') === 'true';
  const all = [...state.villages.values()];
  const busyCount = (v) => agentsOf(v).filter((a) => !isFinished(a) && a.state !== 'idle').length;
  const visible = showAll ? all : all.filter((v) => visibleAgents(v).length > 0);
  return visible.sort((a, b) =>
    busyCount(b) - busyCount(a) || (b.last_active || 0) - (a.last_active || 0));
}

/** Rebuild the grid order, reusing every existing village node. */
function reflowWorld() {
  const villages = sortedVillages();
  if (!villages.length) {
    for (const slug of [...nodes.keys()]) destroyVillage(slug);
    els.world.innerHTML = '';
    els.world.appendChild(emptyState());
    return;
  }
  const empty = els.world.querySelector('.cv-empty');
  if (empty) empty.remove();

  const wanted = new Set(villages.map((v) => v.slug));
  for (const slug of [...nodes.keys()]) if (!wanted.has(slug)) destroyVillage(slug);

  for (const village of villages) {
    // Appending a node that is already in the DOM moves it, keeping its canvas.
    els.world.appendChild(ensureVillage(village).root);
  }
}

function ensureVillage(village) {
  let node = nodes.get(village.slug);
  if (node) return node;

  const root = document.createElement('button');
  root.type = 'button';
  root.className = 'cv-island';
  root.dataset.village = village.slug;
  root.innerHTML = `
    <div class="cv-land">
      <div class="cv-plate">
        <div class="cv-plate__turf"></div>
        <div class="cv-plate__head">
          <span class="cv-sign"></span>
          <span class="cv-plate__meta"><span class="cv-badge"></span></span>
        </div>
        <div class="cv-plate__path"></div>
        <div class="cv-stage">
          <canvas class="cv-canvas" width="${LOGICAL_W}" height="${LOGICAL_H}"></canvas>
          <div class="cv-bubble" hidden></div>
        </div>
      </div>
      ${rockSvg()}
    </div>`;

  const scene = new Scene(root.querySelector('.cv-canvas'), { biome: village.biome });
  registerScene(scene);

  node = {
    root,
    scene,
    sign: root.querySelector('.cv-sign'),
    badge: root.querySelector('.cv-badge'),
    path: root.querySelector('.cv-plate__path'),
    bubble: root.querySelector('.cv-bubble'),
  };
  root.addEventListener('click', () => openDrawer(village.slug));
  nodes.set(village.slug, node);
  return node;
}

function destroyVillage(slug) {
  const node = nodes.get(slug);
  if (!node) return;
  unregisterScene(node.scene);
  node.root.remove();
  nodes.delete(slug);
}

function updateVillage(slug) {
  const village = state.villages.get(slug);
  const node = nodes.get(slug);
  if (!village || !node) return;

  const agents = visibleAgents(village);
  const busy = agents.filter((a) => !isFinished(a) && a.state !== 'idle');

  node.root.classList.toggle('cv-island--quiet', busy.length === 0);
  node.root.classList.toggle('cv-island--gone', village.exists === false);
  node.root.setAttribute('aria-label', `${village.name}: ${busy.length} working`);

  setText(node.sign, village.name);
  node.sign.title = village.name;
  setText(node.path, village.path || 'path unknown');
  node.path.title = village.path || '';

  setText(node.badge, busy.length ? `${busy.length} working` : 'quiet');
  node.badge.classList.toggle('cv-badge--idle', busy.length === 0);

  node.scene.biome = village.biome || node.scene.biome;
  node.scene.syncAgents(agents);

  // One bubble per village: with a workflow running, one per villager would
  // cover the whole island and its neighbours.
  const speaker = agents.find((a) => a.state === 'working' && a.tool);
  if (speaker) {
    node.bubble.hidden = false;
    node.bubble.classList.toggle('cv-bubble--error', speaker.lastOk === false);
    node.bubble.innerHTML =
      `<span class="cv-bubble__tool">${escapeHtml(speaker.tool.display || '')}</span>`
      + escapeHtml(truncate(speaker.tool.label || speaker.tool.display || '', 70));
  } else {
    node.bubble.hidden = true;
  }
}

function setText(el, value) {
  if (el.textContent !== value) el.textContent = value;
}

function rockSvg() {
  return `<svg class="cv-land__rock" viewBox="0 0 200 62" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 0 H200 L186 20 Q168 34 142 33 L116 54 Q104 66 94 54 L66 32 Q34 30 14 17 Z"
          fill="var(--rock)"/>
    <path d="M0 0 H200 L186 20 Q168 34 142 33 L128 44 Q96 40 66 32 Q34 30 14 17 Z"
          fill="var(--rock-deep)" opacity=".45"/>
  </svg>`;
}

function emptyState() {
  const div = document.createElement('div');
  div.className = 'cv-empty';
  div.innerHTML = `
    ${avatarSvg('mayor', 64)}
    <h2>The villages are quiet</h2>
    <p>Start Claude Code in any repository and its village will appear here,
       with a villager for every agent at work.</p>`;
  return div;
}

/* --------------------------------------------------------------- drawer */

function openDrawer(slug) {
  const village = state.villages.get(slug);
  if (!village) return;
  els.drawerTitle.textContent = village.name;
  const agents = agentsOf(village);
  els.roster.innerHTML = agents.length
    ? agents.map((agent) => {
        const species = speciesFor(agent.agent_type);
        const doing = agent.tool
          ? `${escapeHtml(agent.tool.display || '')} · ${escapeHtml(truncate(agent.tool.label || '', 70))}`
          : escapeHtml(truncate(agent.say || '—', 90));
        return `<div class="cv-card">
            ${avatarSvg(species, 34)}
            <div>
              <div class="cv-card__name">${escapeHtml(agent.description || speciesLabel(species))}</div>
              <div class="cv-card__tag">${escapeHtml(agent.state || '')}${agent.phase ? ' · ' + escapeHtml(agent.phase) : ''}</div>
              <div class="cv-card__what">${doing}</div>
            </div>
          </div>`;
      }).join('')
    : `<p class="cv-card__what">No villagers here right now.</p>`;
  els.drawer.dataset.open = 'true';
}

function closeDrawer() { els.drawer.dataset.open = 'false'; }

/* --------------------------------------------------------------- legend */

function paintLegend() {
  els.legend.innerHTML = Object.entries(SPECIES).map(([key, meta]) => `
    <div class="cv-legend__item">
      ${avatarSvg(key, 30)}
      <div>
        <div class="cv-legend__name">${escapeHtml(meta.label)}</div>
        <div class="cv-legend__blurb">${escapeHtml(meta.blurb)}</div>
      </div>
    </div>`).join('');
}

/* ----------------------------------------------------------------- theme */

const THEME_KEY = 'codeville.theme';
const THEME_ICONS = { system: '🌗', light: '🌙', dark: '☀️' };

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
  else root.removeAttribute('data-theme');
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = THEME_ICONS[theme] || THEME_ICONS.system;
  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* ignore */ }
}

function storedTheme() {
  if (['light', 'dark', 'system'].includes(THEME_OVERRIDE)) return THEME_OVERRIDE;
  try { return localStorage.getItem(THEME_KEY) || 'system'; } catch (_) { return 'system'; }
}

function cycleTheme() {
  const order = ['system', 'light', 'dark'];
  applyTheme(order[(order.indexOf(storedTheme()) + 1) % order.length]);
}

/* --------------------------------------------------------------- utils */

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function truncate(text, max) {
  const s = String(text || '');
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

function showTokenHelp() {
  els.world.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'cv-empty';
  div.innerHTML = `
    <h2>Missing access token</h2>
    <p>Codeville only answers authenticated requests. Open it from the tray icon,
       or run <code>codeville --print-url</code> and use the printed link.</p>`;
  els.world.appendChild(div);
}

/* ----------------------------------------------------------------- boot */

function boot() {
  els.world = document.getElementById('world');
  els.stats = document.getElementById('stats');
  els.conn = document.getElementById('conn');
  els.drawer = document.getElementById('drawer');
  els.drawerTitle = document.getElementById('drawer-title');
  els.roster = document.getElementById('roster');
  els.legend = document.getElementById('legend-grid');
  els.toggleAll = document.getElementById('toggle-all');

  preloadSprites();
  paintLegend();
  paintStats();
  applyTheme(storedTheme());

  if (SHOW_ALL_OVERRIDE) els.toggleAll.setAttribute('aria-pressed', 'true');
  els.toggleAll.addEventListener('click', () => {
    const on = els.toggleAll.getAttribute('aria-pressed') === 'true';
    els.toggleAll.setAttribute('aria-pressed', String(!on));
    dirty.add('*');
    queueRepaint();
  });
  document.getElementById('toggle-theme').addEventListener('click', cycleTheme);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  window.addEventListener('resize', () => {
    for (const node of nodes.values()) node.scene.resize();
  });

  connect();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
