/**
 * The village scene: a pixel-art canvas where villagers walk around and work.
 *
 * This is a real little simulation rather than a list with animations bolted on.
 * Each agent owns an Actor with a position, a heading and a destination. When the
 * daemon says an agent started a `Bash` call, that agent's actor *walks to the
 * terminal desk* and works there until the call ends, then wanders off. Watching
 * the island tells you what is happening without reading a word.
 *
 * Rendering notes:
 * - One canvas per village, drawn at a small logical resolution (320x180) and
 *   scaled by an integer factor with smoothing off, so everything stays crisp
 *   pixel art at any size.
 * - Sprites are generated PNGs loaded once and shared across every scene.
 *   Anything missing falls back to a procedurally drawn blob, so the village
 *   still works before the art exists.
 * - One shared requestAnimationFrame loop drives every visible scene; scenes
 *   that scroll out of view are skipped, and the whole loop stops when the tab
 *   is hidden.
 */

export const LOGICAL_W = 320;
export const LOGICAL_H = 120;

/** Ground line the actors stand on. */
const FLOOR_Y = 92;

/** Where each kind of work happens, in logical pixels. */
export const STATIONS = {
  run:    { x: 54,  y: FLOOR_Y, label: 'terminal' },
  edit:   { x: 104, y: FLOOR_Y, label: 'forge' },
  read:   { x: 154, y: FLOOR_Y, label: 'library' },
  write:  { x: 198, y: FLOOR_Y, label: 'desk' },
  search: { x: 240, y: FLOOR_Y, label: 'lookout' },
  web:    { x: 278, y: FLOOR_Y, label: 'telescope' },
  plan:   { x: 132, y: FLOOR_Y, label: 'board' },
  summon: { x: 176, y: FLOOR_Y, label: 'gate' },
  ask:    { x: 80,  y: FLOOR_Y, label: 'bell' },
  mcp:    { x: 258, y: FLOOR_Y, label: 'mast' },
  other:  { x: 216, y: FLOOR_Y, label: 'workshop' },
};

/** Spots villagers drift to when they have nothing to do. */
const IDLE_SPOTS = [
  { x: 40, y: FLOOR_Y }, { x: 88, y: FLOOR_Y }, { x: 140, y: FLOOR_Y },
  { x: 190, y: FLOOR_Y }, { x: 236, y: FLOOR_Y }, { x: 284, y: FLOOR_Y },
];

const WALK_SPEED = 38;       // logical px per second

/** On-screen height of a villager, in logical pixels. */
const ACTOR_SIZE = 34;

/** Ideal gap between two villagers standing at the same place. */
const SLOT_SPACING = 17;

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const ARRIVE_EPSILON = 1.5;

/* ----------------------------------------------------------- sprite bank */

const sprites = new Map();   // name -> {img, ready}

/**
 * Load a sprite once, shared by every scene. Never rejects: a missing file just
 * leaves `ready` false and the actor falls back to a drawn blob.
 */
export function loadSprite(name, url) {
  if (sprites.has(name)) return sprites.get(name);
  const entry = { img: new Image(), ready: false };
  entry.img.addEventListener('load', () => { entry.ready = true; });
  entry.img.addEventListener('error', () => { entry.ready = false; });
  entry.img.src = url;
  sprites.set(name, entry);
  return entry;
}

export function getSprite(name) {
  return sprites.get(name) || null;
}

/* ------------------------------------------------------------------ actor */

/** Deterministic per-id jitter in [0,1), so actors don't move in lockstep.
 *
 * An FNV-style mix rather than the obvious `h*31 + c`: short, similar ids
 * ("a1", "a2", "a3") land in almost the same place with the simple version, so
 * every actor entered from the same side at the same moment. */
function hashUnit(id) {
  let h = 2166136261;
  for (let i = 0; i < id.length; i += 1) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

export class Actor {
  constructor(agent) {
    this.id = agent.id;
    this.species = agent.species || 'villager';
    this.seed = hashUnit(this.id);
    const spot = IDLE_SPOTS[Math.floor(this.seed * IDLE_SPOTS.length)];
    // Enter from whichever edge is nearer, so arrivals read as arrivals.
    this.x = this.seed < 0.5 ? -14 : LOGICAL_W + 14;
    this.y = spot.y;
    this.targetX = spot.x;
    this.facing = this.x < this.targetX ? 1 : -1;
    this.state = 'walking';
    this.phase = this.seed * Math.PI * 2;
    this.working = false;
    this.station = null;
    this.alpha = 0;
    this.leaving = false;
    this.flash = 0;           // brief highlight on success/failure
    this.flashOk = true;
  }

  /** Point the actor at the station for a tool category. */
  goToWork(category) {
    this.station = category;
    this.working = true;
    this.state = 'walking';
  }

  /** Send the actor back to loitering. */
  goIdle() {
    this.station = null;
    this.working = false;
    this.state = 'walking';
  }

  /** Where this actor should stand, given its slot among others like it. */
  placeAt(baseX, slot, total) {
    // Fan out around the anchor so a crowd at one station stays legible instead
    // of collapsing into a single sprite.
    const spread = Math.min(SLOT_SPACING, 70 / Math.max(1, total - 1));
    this.targetX = clamp(baseX + (slot - (total - 1) / 2) * spread, 10, LOGICAL_W - 10);
  }

  finish(ok) {
    this.flash = 1;
    this.flashOk = ok;
    this.leaving = true;
    this.targetX = this.x < LOGICAL_W / 2 ? -20 : LOGICAL_W + 20;
    this.state = 'walking';
    this.working = false;
  }

  update(dt) {
    this.alpha = Math.min(1, this.alpha + dt * 3);
    if (this.flash > 0) this.flash = Math.max(0, this.flash - dt * 1.4);

    const dx = this.targetX - this.x;
    if (Math.abs(dx) > ARRIVE_EPSILON) {
      this.facing = dx > 0 ? 1 : -1;
      this.x += Math.sign(dx) * Math.min(Math.abs(dx), WALK_SPEED * dt);
      this.state = 'walking';
    } else {
      this.x = this.targetX;
      this.state = this.working ? 'working' : 'idle';
    }
    this.phase += dt * (this.state === 'working' ? 9 : this.state === 'walking' ? 8 : 2.2);
  }

  /**
   * Which cell of the sprite sheet to show.
   *
   * A sheet is a horizontal strip of square cells, so the frame count is simply
   * width/height. Walking runs the full cycle; working alternates the two
   * contact poses for a busier motion; anything else rests on frame 0.
   */
  frameIndex(img) {
    const frames = Math.max(1, Math.round(img.width / img.height));
    if (frames === 1) return 0;
    const beat = Math.floor(this.phase / (Math.PI / 2));
    if (this.state === 'walking') return ((beat % frames) + frames) % frames;
    if (this.state === 'working') return (((beat % 2) + 2) % 2) * (frames > 2 ? 2 : 1);
    return 0;
  }

  /** Put the actor on its mark immediately, skipping the walk-in. */
  snapToTarget() {
    this.x = this.targetX;
    this.alpha = 1;
    this.state = this.working ? 'working' : 'idle';
  }

  get gone() {
    return this.leaving && (this.x < -18 || this.x > LOGICAL_W + 18);
  }

  draw(ctx) {
    const bobbing = this.state === 'walking'
      ? Math.abs(Math.sin(this.phase)) * 2.2
      : Math.sin(this.phase) * 0.7;
    const y = this.y - bobbing;

    ctx.save();
    ctx.globalAlpha = this.alpha;

    // Contact shadow keeps the actor grounded.
    ctx.fillStyle = 'rgba(24, 18, 48, 0.22)';
    ctx.beginPath();
    ctx.ellipse(this.x, this.y + 1, 7, 2.4, 0, 0, Math.PI * 2);
    ctx.fill();

    const entry = sprites.get(this.species);
    ctx.translate(this.x, y);
    if (this.facing < 0) ctx.scale(-1, 1);

    if (this.state === 'working') {
      // A small lean into the work, plus a nod on the beat.
      ctx.rotate(Math.sin(this.phase) * 0.06);
    }

    if (entry && entry.ready) {
      drawSheetFrame(ctx, entry.img, this.frameIndex(entry.img), ACTOR_SIZE);
    } else {
      drawFallbackBody(ctx, this.species);
    }
    ctx.restore();

    if (this.flash > 0) {
      ctx.save();
      ctx.globalAlpha = this.flash * 0.9;
      ctx.fillStyle = this.flashOk ? '#7CF5A0' : '#FF6B7A';
      ctx.font = '9px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(this.flashOk ? '✓' : '✕', this.x, y - 26 - (1 - this.flash) * 8);
      ctx.restore();
    }
  }
}

/** Draw one square cell out of a horizontal sprite strip, bottom-centred. */
function drawSheetFrame(ctx, img, frame, size) {
  const cell = img.height;
  ctx.drawImage(img, frame * cell, 0, cell, cell, -size / 2, -size, size, size);
}

/** Palette used only when a generated sprite has not loaded. */
const FALLBACK_COLORS = {
  mayor: '#E6E8FF', worker: '#9BE3F2', explorer: '#F0D9A8', planner: '#B98A5E',
  reviewer: '#FCFCFF', generalist: '#FFA45B', guide: '#C7B9FF',
  tinkerer: '#D8D8E0', villager: '#FFC2D1',
};

function drawFallbackBody(ctx, species) {
  const body = FALLBACK_COLORS[species] || FALLBACK_COLORS.villager;
  ctx.fillStyle = body;
  ctx.strokeStyle = 'rgba(40,32,80,.85)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(-7, -18, 14, 18, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = '#23243A';
  ctx.fillRect(-4, -13, 2.5, 2.5);
  ctx.fillRect(1.5, -13, 2.5, 2.5);
}

/* ------------------------------------------------------------------ scene */

export class Scene {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: true });
    this.ctx.imageSmoothingEnabled = false;
    this.actors = new Map();
    this.biome = options.biome || 'meadow';
    this.backdrop = options.backdrop || null;
    this.time = 0;
    //: False until the first agent list arrives, so the opening cast can be
    //: placed rather than marched in.
    this.primed = false;
    this.resize();
  }

  resize() {
    // Integer scale keeps pixels square; devicePixelRatio keeps it sharp.
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const scale = Math.max(1, Math.floor((rect.width * dpr) / LOGICAL_W)) || 1;
    this.canvas.width = LOGICAL_W * scale;
    this.canvas.height = LOGICAL_H * scale;
    this.scale = scale;
    this.ctx.imageSmoothingEnabled = false;
  }

  syncAgents(agents) {
    const seen = new Set();
    const opening = !this.primed;
    const freshActors = [];
    for (const agent of agents) {
      seen.add(agent.id);
      let actor = this.actors.get(agent.id);
      const fresh = !actor;
      if (fresh) {
        actor = new Actor(agent);
        this.actors.set(agent.id, actor);
      }
      actor.species = agent.species || actor.species;

      if (agent.state === 'done' || agent.state === 'failed') {
        if (!actor.leaving) actor.finish(agent.state === 'done');
      } else if (agent.state === 'working' && agent.tool) {
        const category = agent.tool.category || 'other';
        if (actor.station !== category) actor.goToWork(category);
      } else if (actor.working) {
        actor.goIdle();
      }

      // The villagers already living here are standing about when you arrive;
      // only agents that start *later* walk in through the gate. Otherwise the
      // village looks deserted for the first few seconds after every connect.
      if (fresh && opening) freshActors.push(actor);
    }
    for (const [id, actor] of this.actors) {
      if (!seen.has(id) && !actor.leaving) actor.finish(true);
    }

    this.layoutStations();
    // Place the opening cast only after their marks are known.
    for (const actor of freshActors) actor.snapToTarget();
    this.primed = true;
  }

  /**
   * Give every actor a spot, fanning out anyone sharing a destination.
   *
   * Without this two agents running Bash at once stand in exactly the same
   * place and read as one villager — which is how a village with "2 working"
   * ended up showing a single character.
   */
  layoutStations() {
    const groups = new Map();
    for (const actor of this.actors.values()) {
      if (actor.leaving) continue;
      const key = actor.station || `idle:${Math.floor(actor.seed * IDLE_SPOTS.length)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(actor);
    }
    for (const [key, members] of groups) {
      members.sort((a, b) => (a.id < b.id ? -1 : 1));   // stable, not frame-dependent
      const anchor = key.startsWith('idle:')
        ? IDLE_SPOTS[Number(key.slice(5))] || IDLE_SPOTS[0]
        : (STATIONS[key] || STATIONS.other);
      members.forEach((actor, index) => actor.placeAt(anchor.x, index, members.length));
    }
  }

  update(dt) {
    this.time += dt;
    for (const [id, actor] of this.actors) {
      actor.update(dt);
      if (actor.gone) this.actors.delete(id);
    }
  }

  draw() {
    const ctx = this.ctx;
    ctx.setTransform(this.scale, 0, 0, this.scale, 0, 0);
    ctx.clearRect(0, 0, LOGICAL_W, LOGICAL_H);

    if (this.backdrop && this.backdrop.ready) {
      ctx.drawImage(this.backdrop.img, 0, 0, LOGICAL_W, LOGICAL_H);
    } else {
      drawFallbackGround(ctx, this.biome, this.time);
    }

    const occupied = new Set();
    for (const actor of this.actors.values()) {
      if (actor.station && !actor.leaving) occupied.add(actor.station);
    }
    drawStations(ctx, occupied);

    // Painter's order: actors lower on screen draw in front.
    const ordered = [...this.actors.values()].sort((a, b) => a.y - b.y || a.x - b.x);
    for (const actor of ordered) actor.draw(ctx);
  }
}

function drawFallbackGround(ctx, biome, time) {
  const sky = ctx.createLinearGradient(0, 0, 0, LOGICAL_H);
  sky.addColorStop(0, '#a9dcff');
  sky.addColorStop(1, '#dcd2ff');
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, LOGICAL_W, LOGICAL_H);

  // Two parallax hill bands.
  ctx.fillStyle = '#bfe8c8';
  ctx.beginPath();
  ctx.moveTo(0, FLOOR_Y - 6);
  for (let x = 0; x <= LOGICAL_W; x += 8) {
    ctx.lineTo(x, FLOOR_Y - 12 + Math.sin((x + time * 2) * 0.03) * 4);
  }
  ctx.lineTo(LOGICAL_W, LOGICAL_H); ctx.lineTo(0, LOGICAL_H); ctx.fill();

  ctx.fillStyle = '#8fd69a';
  ctx.fillRect(0, FLOOR_Y, LOGICAL_W, LOGICAL_H - FLOOR_Y);
  ctx.fillStyle = '#7bc487';
  for (let x = 0; x < LOGICAL_W; x += 6) ctx.fillRect(x, FLOOR_Y, 3, 2);
}

/** Colours for the drawn station markers, used when a sprite is missing. */
const STATION_COLORS = {
  run: '#2B2D42', edit: '#8D99AE', read: '#B08968', write: '#C79A4B',
  search: '#4A7FA5', web: '#5B5F97', plan: '#9AA0B5', summon: '#B98A2B',
  ask: '#7C5CFF', mcp: '#3E6D99', other: '#6B7280',
};

/**
 * Props marking each station.
 *
 * Only stations that something is actually happening at are drawn, so a quiet
 * village is a quiet field rather than a row of unexplained furniture. A station
 * without generated art still gets a simple drawn marker — otherwise villagers
 * walk purposefully towards nothing.
 */
function drawStations(ctx, occupied) {
  for (const key of occupied) {
    const station = STATIONS[key];
    if (!station) continue;
    const entry = sprites.get(`station_${key}`);
    if (entry && entry.ready) {
      ctx.save();
      ctx.translate(station.x, station.y);
      drawSheetFrame(ctx, entry.img, 0, 26);
      ctx.restore();
      continue;
    }
    ctx.save();
    ctx.fillStyle = 'rgba(24, 18, 48, 0.16)';
    ctx.beginPath();
    ctx.ellipse(station.x, station.y + 1, 13, 3, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = STATION_COLORS[key] || STATION_COLORS.other;
    ctx.fillRect(station.x - 8, station.y - 11, 16, 11);
    ctx.fillStyle = 'rgba(255,255,255,.35)';
    ctx.fillRect(station.x - 5, station.y - 8, 10, 2);
    ctx.restore();
  }
}

/* ------------------------------------------------------- the shared loop */

const liveScenes = new Set();
let running = false;
let lastFrame = 0;

export function registerScene(scene) { liveScenes.add(scene); startLoop(); }
export function unregisterScene(scene) { liveScenes.delete(scene); }

function startLoop() {
  if (running) return;
  running = true;
  lastFrame = performance.now();
  requestAnimationFrame(step);
}

function step(now) {
  // No document.hidden check here: requestAnimationFrame already stops firing in
  // a background tab, so the guard bought nothing — and it silently froze the
  // whole world in any context that reports itself hidden, such as a headless
  // browser, leaving an empty village behind.
  const dt = Math.min(0.05, (now - lastFrame) / 1000);
  lastFrame = now;
  for (const scene of liveScenes) {
    if (!isOnScreen(scene.canvas)) continue;           // skip scrolled-away villages
    scene.update(dt);
    scene.draw();
  }
  requestAnimationFrame(step);
}

function isOnScreen(el) {
  const r = el.getBoundingClientRect();
  return r.bottom > -80 && r.top < window.innerHeight + 80;
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && liveScenes.size) { running = false; startLoop(); }
});
