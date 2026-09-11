/**
 * Codeville front end.
 *
 * Holds a mirror of the daemon's world, applies the delta events it streams, and
 * repaints. The daemon is the single source of truth: this module never invents
 * state, it only reflects what Claude Code actually did.
 *
 * Rendering is deliberately dumb-but-targeted. A village repaints only when one of
 * its own villagers changes, so a busy workflow in one repo does not cause the
 * other 28 villages to re-render.
 */

import { avatarSvg, characterSvg, PROPS, SPECIES, speciesFor, speciesLabel } from './characters.js';

/* --------------------------------------------------------------- state */

const state = {
  villages: new Map(),   // slug -> village (with sessions[])
  agents: new Map(),     // agentId -> {agent, villageSlug}
  stats: {},
  connected: false,
};

const els = {};
const dirtyVillages = new Set();
let repaintQueued = false;
let bubbleTimers = new Map();

/* ---------------------------------------------------------- connection */

function tokenFromLocation() {
  const fromQuery = new URLSearchParams(location.search).get('token');
  if (fromQuery) {
    // Keep it out of the visible URL and out of history once we have it.
    try {
      sessionStorage.setItem('codeville.token', fromQuery);
      history.replaceState(null, '', location.pathname);
    } catch (_) { /* private mode: carry on with the in-memory value */ }
    return fromQuery;
  }
  try { return sessionStorage.getItem('codeville.token') || ''; } catch (_) { return ''; }
}

/** ?theme=light|dark|system overrides the stored choice — handy for screenshots
 *  and for embedding the village in a kiosk. Read before the query string is
 *  cleared by tokenFromLocation(). */
const THEME_OVERRIDE = new URLSearchParams(location.search).get('theme');

const TOKEN = tokenFromLocation();
let socket = null;
let reconnectDelay = 500;

function connect() {
  if (!TOKEN) { setConnection('down', 'no token'); showTokenHelp(); return; }
  const url = `ws://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`;
  setConnection('wait', 'connecting');

  try { socket = new WebSocket(url); }
  catch (err) { scheduleReconnect(); return; }

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
  state.connected = kind === 'live';
  if (!els.conn) return;
  els.conn.className = `cv-conn cv-conn--${kind === 'live' ? 'live' : kind === 'down' ? 'down' : 'wait'}`;
  els.conn.querySelector('.cv-conn__text').textContent = label;
}

/* -------------------------------------------------------------- events */

function handleEvent(event) {
  switch (event.t) {
    case 'hello':
      loadSnapshot(event);
      break;
    case 'village.upsert':
      upsertVillage(event.village);
      break;
    case 'session.update':
      withSession(event.village, event.session, (s) => { if (event.title) s.title = event.title; });
      break;
    case 'session.end':
      removeSession(event.village, event.session);
      break;
    case 'agent.spawn':
    case 'agent.update':
      upsertAgent(event.agent);
      break;
    case 'agent.state':
      patchAgent(event.agent, (a) => { a.state = event.state; if (event.state !== 'working') a.tool = null; });
      break;
    case 'agent.tool':
      patchAgent(event.agent, (a) => { a.tool = event.tool; a.state = 'working'; });
      break;
    case 'agent.tool_end':
      patchAgent(event.agent, (a) => { a.lastOk = event.ok; });
      break;
    case 'agent.say':
      patchAgent(event.agent, (a) => { a.say = event.text; });
      break;
    case 'agent.done':
      patchAgent(event.agent, (a) => { a.state = event.ok ? 'done' : 'failed'; a.tool = null; });
      break;
    case 'agent.despawn':
      removeAgent(event.agent);
      break;
    case 'workflow.start':
    case 'workflow.update':
      withSession(event.village, event.session, (s) => {
        s.workflows = (s.workflows || []).filter((w) => w.run_id !== event.workflow.run_id);
        s.workflows.push(event.workflow);
      });
      break;
    default:
      return;
  }
  recomputeStats();
  queueRepaint();
}

function loadSnapshot(snapshot) {
  state.villages.clear();
  state.agents.clear();
  for (const village of snapshot.villages || []) upsertVillage(village, true);
  state.stats = snapshot.stats || {};
  dirtyVillages.add('*');
  queueRepaint();
}

function upsertVillage(incoming, withSessions = false) {
  const existing = state.villages.get(incoming.slug);
  if (!existing) {
    const village = { ...incoming, sessions: incoming.sessions || [] };
    state.villages.set(village.slug, village);
    indexVillageAgents(village);
    dirtyVillages.add('*');
    return;
  }
  Object.assign(existing, { ...incoming, sessions: withSessions && incoming.sessions ? incoming.sessions : existing.sessions });
  if (withSessions && incoming.sessions) indexVillageAgents(existing);
  dirtyVillages.add(existing.slug);
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
  dirtyVillages.add(slug);
}

function upsertAgent(agent) {
  if (!agent || !agent.id) return;
  const known = state.agents.get(agent.id);
  if (known) {
    Object.assign(known.agent, agent);
    dirtyVillages.add(known.villageSlug);
    return;
  }
  // A brand-new agent: attach it to its session, creating it if the session
  // event has not arrived yet (events can interleave).
  const slug = findVillageForSession(agent.session);
  if (!slug) return;
  withSession(slug, agent.session, (session) => {
    session.villagers = session.villagers || [];
    session.villagers.push(agent);
  });
  state.agents.set(agent.id, { agent, villageSlug: slug });
  dirtyVillages.add(slug);
}

function findVillageForSession(sessionId) {
  for (const village of state.villages.values()) {
    if ((village.sessions || []).some((s) => s.id === sessionId)) return village.slug;
  }
  // Unknown session: park it in the most recently active village so the agent is
  // still visible rather than silently dropped.
  const newest = [...state.villages.values()].sort((a, b) => (b.last_active || 0) - (a.last_active || 0))[0];
  return newest ? newest.slug : null;
}

function patchAgent(agentId, fn) {
  const known = state.agents.get(agentId);
  if (!known) return;
  fn(known.agent);
  dirtyVillages.add(known.villageSlug);
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
  dirtyVillages.add(known.villageSlug);
}

function removeSession(slug, sessionId) {
  const village = state.villages.get(slug);
  if (!village) return;
  const session = (village.sessions || []).find((s) => s.id === sessionId);
  for (const agent of (session && session.villagers) || []) state.agents.delete(agent.id);
  village.sessions = (village.sessions || []).filter((s) => s.id !== sessionId);
  dirtyVillages.add(slug);
}

function recomputeStats() {
  let working = 0;
  let live = 0;
  for (const village of state.villages.values()) {
    const busy = agentsOf(village).filter((a) => !isFinished(a)).length;
    if (busy) live += 1;
    working += agentsOf(village).filter((a) => a.state === 'working').length;
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
 * Which villagers actually earn a spot on the green.
 *
 * A village accumulates one mayor per session, so an old repo can end up with a
 * row of identical dozing mayors that say nothing about what is happening. Show
 * everyone who is doing something, plus the current session's mayor even when it
 * is waiting on you — and leave the rest off the green. They are all still listed
 * in the drawer.
 */
function visibleAgents(village) {
  const sessions = [...(village.sessions || [])]
    .sort((a, b) => (b.last_activity || 0) - (a.last_activity || 0));
  const newest = sessions[0];
  const keep = new Map();

  for (const session of sessions) {
    for (const agent of session.villagers || []) {
      if (agent.state !== 'idle') keep.set(agent.id, agent);
    }
  }
  for (const agent of (newest && newest.villagers) || []) {
    if (agent.agent_type === 'mayor') keep.set(agent.id, agent);
  }
  return [...keep.values()].sort(
    (a, b) => (STATE_ORDER[a.state] ?? 9) - (STATE_ORDER[b.state] ?? 9));
}

/* ------------------------------------------------------------ rendering */

function queueRepaint() {
  if (repaintQueued) return;
  repaintQueued = true;
  requestAnimationFrame(() => {
    repaintQueued = false;
    paint();
  });
}

function paint() {
  paintStats();
  const full = dirtyVillages.has('*');
  if (full) {
    paintWorld();
  } else {
    for (const slug of dirtyVillages) paintVillage(slug);
  }
  dirtyVillages.clear();
}

function paintStats() {
  if (!els.stats) return;
  const s = state.stats || {};
  const items = [
    ['villages', s.villages || 0, 'villages'],
    ['live', s.villages_live || 0, 'busy'],
    ['agents', s.agents_working || 0, 'working'],
    ['tools', s.tool_calls || 0, 'tool calls'],
  ];
  els.stats.innerHTML = items.map(([key, value, label]) => `
    <div class="cv-stat ${key === 'live' || key === 'agents' ? 'cv-stat--live' : ''}">
      <span class="cv-stat__value">${formatNumber(value)}</span>
      <span class="cv-stat__label">${label}</span>
    </div>`).join('');
}

function formatNumber(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
  return String(n);
}

function sortedVillages() {
  const showAll = els.toggleAll && els.toggleAll.getAttribute('aria-pressed') === 'true';
  const all = [...state.villages.values()];
  const visible = showAll ? all : all.filter((v) => agentsOf(v).length > 0 || v.busy > 0);
  return visible.sort((a, b) => {
    const busyDiff = agentsOf(b).filter((x) => !isFinished(x)).length
                   - agentsOf(a).filter((x) => !isFinished(x)).length;
    if (busyDiff) return busyDiff;
    return (b.last_active || 0) - (a.last_active || 0);
  });
}

function paintWorld() {
  const villages = sortedVillages();
  if (!villages.length) {
    els.world.innerHTML = '';
    els.world.appendChild(emptyState());
    return;
  }
  els.world.innerHTML = '';
  villages.forEach((village, index) => {
    els.world.appendChild(villageElement(village, index));
  });
}

function paintVillage(slug) {
  const village = state.villages.get(slug);
  const node = els.world.querySelector(`[data-village="${cssEscape(slug)}"]`);
  if (!village || !node) { paintWorld(); return; }
  const index = Number(node.dataset.index || 0);
  const replacement = villageElement(village, index);
  node.replaceWith(replacement);
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

function villageElement(village, index) {
  const agents = visibleAgents(village);
  const busy = agentsOf(village).filter((a) => !isFinished(a) && a.state !== 'idle');
  const node = document.createElement('button');
  node.className = 'cv-island'
    + (busy.length ? '' : ' cv-island--quiet')
    + (village.exists === false ? ' cv-island--gone' : '');
  node.dataset.village = village.slug;
  node.dataset.index = String(index);
  node.style.setProperty('--float-delay', `${(index % 5) * 0.55}s`);
  node.type = 'button';
  node.setAttribute('aria-label', `${village.name}: ${busy.length} working`);

  node.innerHTML = `
    <div class="cv-land">
      <div class="cv-plate">
        <div class="cv-plate__turf"></div>
        <div class="cv-plate__head">
          <span class="cv-sign" title="${escapeHtml(village.name)}">${escapeHtml(village.name)}</span>
          <span class="cv-plate__meta">
            ${busy.length
              ? `<span class="cv-badge">${busy.length} working</span>`
              : `<span class="cv-badge cv-badge--idle">quiet</span>`}
          </span>
        </div>
        <div class="cv-plate__path" title="${escapeHtml(village.path || '')}">${escapeHtml(village.path || 'path unknown')}</div>
        <div class="cv-green"></div>
      </div>
      ${rockSvg()}
    </div>`;

  const green = node.querySelector('.cv-green');
  green.insertAdjacentHTML('afterbegin', scenerySvg(village.biome));

  // One speech bubble per village, not one per villager: with a workflow running
  // a dozen bubbles would cover the whole island and overlap its neighbours.
  const speaker = agents.find((a) => a.state === 'working' && a.tool);
  if (speaker) {
    const bubble = document.createElement('div');
    bubble.className = 'cv-bubble' + (speaker.lastOk === false ? ' cv-bubble--error' : '');
    bubble.innerHTML =
      `<span class="cv-bubble__tool">${escapeHtml(speaker.tool.display || '')}</span>`
      + escapeHtml(truncate(speaker.tool.label || speaker.tool.display || '', 70));
    green.appendChild(bubble);
  }

  if (!agents.length) {
    green.innerHTML = `<div class="cv-green__empty">no one about</div>`;
  } else {
    for (const agent of agents.slice(0, 14)) green.appendChild(villagerElement(agent));
    if (agents.length > 14) {
      const more = document.createElement('div');
      more.className = 'cv-green__empty';
      more.textContent = `+${agents.length - 14} more`;
      green.appendChild(more);
    }
  }

  node.addEventListener('click', () => openDrawer(village.slug));
  return node;
}

/**
 * The underside of a floating island: a lump of rock tapering to a point, with a
 * couple of smaller shards drifting beneath it. Drawn with `preserveAspectRatio`
 * off so it stretches to whatever width the grid gives the card.
 */
function rockSvg() {
  return `<svg class="cv-land__rock" viewBox="0 0 200 62" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 0 H200 L186 20 Q168 34 142 33 L116 54 Q104 66 94 54 L66 32 Q34 30 14 17 Z"
          fill="var(--rock)"/>
    <path d="M0 0 H200 L186 20 Q168 34 142 33 L128 44 Q96 40 66 32 Q34 30 14 17 Z"
          fill="var(--rock-deep)" opacity=".45"/>
    <ellipse cx="42" cy="9" rx="17" ry="5" fill="var(--grass-deep)" opacity=".55"/>
    <ellipse cx="150" cy="10" rx="13" ry="4" fill="var(--grass-deep)" opacity=".4"/>
  </svg>`;
}

/**
 * The land behind the villagers: soft rolling hills in the village's biome colour.
 *
 * Deliberately just a horizon. An earlier version drew trees and a hut, but at card
 * width they scaled into floating lollipops and competed with the characters — who
 * are the thing you are actually meant to be watching.
 */
function scenerySvg(biome) {
  const tint = BIOME_TINTS[biome] || BIOME_TINTS.meadow;
  return `<svg class="cv-scenery" viewBox="0 0 320 90" preserveAspectRatio="none" aria-hidden="true">
    <path d="M0 90 V56 q40-16 82-6 t74 4 q44-14 86-4 t78 12 V90z" fill="${tint.far}"/>
    <path d="M0 90 V70 q52-14 96-4 t84 2 q40-10 76-2 t64 8 V90z" fill="${tint.near}"/>
  </svg>`;
}

/** One palette per biome — the language census picks which a village gets. */
const BIOME_TINTS = {
  meadow:   { far: '#d8f5de', near: '#b4e9bf' },
  forest:   { far: '#cdeed6', near: '#9fdcaf' },
  harbor:   { far: '#d5eefb', near: '#aadcf1' },
  canyon:   { far: '#fbe2cd', near: '#f3c8a4' },
  tundra:   { far: '#e6f3fa', near: '#c8e2ef' },
  citadel:  { far: '#e8e3f8', near: '#cfc6ee' },
  cliffs:   { far: '#ffe6dd', near: '#fcc8b8' },
  orchard:  { far: '#ffe7ef', near: '#ffc6d9' },
  bazaar:   { far: '#fff0d5', near: '#fbdca6' },
  foundry:  { far: '#e6e8ee', near: '#ccd0da' },
  workshop: { far: '#ece8f9', near: '#d3cbee' },
};

function villagerElement(agent) {
  const species = speciesFor(agent.agent_type);
  const node = document.createElement('div');
  node.className = 'cv-villager';
  node.dataset.state = agent.state || 'idle';
  node.dataset.agent = agent.id;
  node.title = describeAgent(agent);

  const category = agent.tool && agent.tool.category ? agent.tool.category : null;
  node.innerHTML = characterSvg({ species, toolCategory: category });

  return node;
}

function describeAgent(agent) {
  const bits = [speciesLabel(speciesFor(agent.agent_type))];
  if (agent.description) bits.push(agent.description);
  if (agent.phase) bits.push(`phase: ${agent.phase}`);
  bits.push(`${agent.tool_count || 0} tool calls`);
  return bits.join(' — ');
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

/* --------------------------------------------------------------- sky */

function paintClouds() {
  const sky = els.sky;
  const count = 7;
  for (let i = 0; i < count; i += 1) {
    const cloud = document.createElement('div');
    cloud.className = 'cv-cloud';
    const height = 26 + (i % 3) * 16;
    cloud.style.height = `${height}px`;
    cloud.style.width = `${height * (2.6 + (i % 4) * 0.5)}px`;
    cloud.style.top = `${6 + i * 12}%`;
    cloud.style.animationDuration = `${70 + i * 22}s`;
    cloud.style.animationDelay = `${-i * 14}s`;
    cloud.style.opacity = String(0.5 + (i % 3) * 0.14);
    sky.appendChild(cloud);
  }
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

function cssEscape(value) {
  return String(value).replace(/["\\]/g, '\\$&');
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

/**
 * Day/night. Three states, matching the CSS: an explicit choice stamps
 * data-theme on <html>, and "system" stamps nothing so prefers-color-scheme
 * decides. The choice is a per-browser convenience, so localStorage is the right
 * home for it — and it is wrapped because private windows can throw on access.
 */
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
  if (THEME_OVERRIDE === 'light' || THEME_OVERRIDE === 'dark' || THEME_OVERRIDE === 'system') {
    return THEME_OVERRIDE;
  }
  try { return localStorage.getItem(THEME_KEY) || 'system'; } catch (_) { return 'system'; }
}

function cycleTheme() {
  const order = ['system', 'light', 'dark'];
  const next = order[(order.indexOf(storedTheme()) + 1) % order.length];
  applyTheme(next);
}

function boot() {
  els.sky = document.getElementById('sky');
  els.world = document.getElementById('world');
  els.stats = document.getElementById('stats');
  els.conn = document.getElementById('conn');
  els.drawer = document.getElementById('drawer');
  els.drawerTitle = document.getElementById('drawer-title');
  els.roster = document.getElementById('roster');
  els.legend = document.getElementById('legend-grid');
  els.toggleAll = document.getElementById('toggle-all');

  paintClouds();
  paintLegend();
  paintStats();

  els.toggleAll.addEventListener('click', () => {
    const on = els.toggleAll.getAttribute('aria-pressed') === 'true';
    els.toggleAll.setAttribute('aria-pressed', String(!on));
    dirtyVillages.add('*');
    queueRepaint();
  });
  applyTheme(storedTheme());
  document.getElementById('toggle-theme').addEventListener('click', cycleTheme);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  connect();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
