/**
 * The cast of Codeville.
 *
 * Every villager is built from one chibi kit so the whole cast reads as a family:
 * same proportions, same outline weight, same eye design. What changes per species
 * is the palette, the head silhouette (ears, horns, crest) and the held prop.
 *
 * Characters are inline SVG rather than generated images on purpose. Each body part
 * is its own <g> with a stable class, so CSS can animate an arm, a head and a prop
 * independently; the result stays crisp at any zoom, re-themes instantly, and costs
 * a few hundred bytes instead of a sprite sheet.
 *
 * Coordinates live in a 64x64 viewBox with the character standing on y=58.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Palette per species: body, shade, accent, and the eye/─ face colour. */
export const SPECIES = {
  mayor: {
    label: 'Mayor',
    blurb: 'runs the show',
    body: '#E6E8FF', shade: '#8286D8', accent: '#7C5CFF', trim: '#FFC233',
    head: 'robot', crown: true, antenna: true,
  },
  explorer: {
    label: 'Scout',
    blurb: 'reads the land',
    body: '#F0D9A8', shade: '#D4B483', accent: '#8B6BD6', trim: '#FFF1C9',
    head: 'owl',
  },
  planner: {
    label: 'Architect',
    blurb: 'draws the plans',
    body: '#B98A5E', shade: '#966C46', accent: '#4FB5A5', trim: '#FFE6C2',
    head: 'beaver',
  },
  reviewer: {
    label: 'Inspector',
    blurb: 'checks the work',
    body: '#FCFCFF', shade: '#9AA3B8', accent: '#2F3542', trim: '#8ED1C4',
    head: 'panda',
  },
  worker: {
    label: 'Worker',
    blurb: 'gets it done',
    body: '#9BE3F2', shade: '#6FC3D6', accent: '#FF8FB1', trim: '#FFFFFF',
    head: 'robot', antenna: true,
  },
  generalist: {
    label: 'Fixer',
    blurb: 'turns a hand to anything',
    body: '#FFA45B', shade: '#E07C36', accent: '#3D3B6B', trim: '#FFE0C2',
    head: 'fox',
  },
  guide: {
    label: 'Guide',
    blurb: 'knows the way',
    body: '#C7B9FF', shade: '#A08BE8', accent: '#FFD166', trim: '#FFFFFF',
    head: 'cat',
  },
  tinkerer: {
    label: 'Tinkerer',
    blurb: 'fiddles with the dials',
    body: '#D8D8E0', shade: '#B0B0C0', accent: '#FF7B54', trim: '#FFFFFF',
    head: 'mouse',
  },
  villager: {
    label: 'Villager',
    blurb: 'pitches in',
    body: '#FFC2D1', shade: '#E89BB0', accent: '#5B5F97', trim: '#FFFFFF',
    head: 'cat',
  },
};

/**
 * Map a Claude Code agentType (or Task subagent_type) onto a species.
 * Unknown types fall back to `villager`, so a new agent kind still shows up.
 */
const AGENT_SPECIES = {
  mayor: 'mayor',
  'workflow-subagent': 'worker',
  'general-purpose': 'generalist',
  claude: 'generalist',
  subagent: 'villager',
  Explore: 'explorer',
  explore: 'explorer',
  Plan: 'planner',
  plan: 'planner',
  'code-reviewer': 'reviewer',
  'code-review': 'reviewer',
  'claude-code-guide': 'guide',
  'statusline-setup': 'tinkerer',
};

export function speciesFor(agentType) {
  if (!agentType) return 'villager';
  if (AGENT_SPECIES[agentType]) return AGENT_SPECIES[agentType];
  const lower = String(agentType).toLowerCase();
  if (AGENT_SPECIES[lower]) return AGENT_SPECIES[lower];
  if (lower.includes('review')) return 'reviewer';
  if (lower.includes('explor') || lower.includes('search')) return 'explorer';
  if (lower.includes('plan') || lower.includes('architect')) return 'planner';
  if (lower.includes('guide') || lower.includes('doc')) return 'guide';
  if (lower.includes('workflow')) return 'worker';
  return 'villager';
}

/* ------------------------------------------------------------------ *
 * Props — what a villager holds while a tool runs.
 * Keyed by the daemon's tool category so an unknown tool still gets one.
 * ------------------------------------------------------------------ */

export const PROPS = {
  run: { label: 'terminal', emoji: '⌨️' },
  read: { label: 'book', emoji: '📖' },
  write: { label: 'quill', emoji: '🪶' },
  edit: { label: 'hammer', emoji: '🔨' },
  search: { label: 'lens', emoji: '🔍' },
  web: { label: 'telescope', emoji: '🔭' },
  plan: { label: 'checklist', emoji: '📋' },
  summon: { label: 'horn', emoji: '📯' },
  ask: { label: 'question', emoji: '❓' },
  mcp: { label: 'socket', emoji: '🔌' },
  other: { label: 'cog', emoji: '⚙️' },
};

function propSvg(category) {
  switch (category) {
    case 'run': // a little terminal screen
      return `<g class="cv-prop cv-prop--run">
        <rect x="40" y="34" width="18" height="13" rx="2.5" fill="#2B2D42" stroke="#14152A" stroke-width="1.5"/>
        <rect x="42" y="36.5" width="7" height="1.6" rx=".8" fill="#7CF5A0"/>
        <rect x="42" y="39.5" width="11" height="1.6" rx=".8" fill="#7CF5A0" opacity=".8"/>
        <rect x="42" y="42.5" width="5" height="1.6" rx=".8" fill="#7CF5A0" opacity=".6"/>
      </g>`;
    case 'read':
      return `<g class="cv-prop cv-prop--read">
        <path d="M40 34 h16 a2 2 0 0 1 2 2 v10 a2 2 0 0 1-2 2 H40z" fill="#FFF6E0" stroke="#B08968" stroke-width="1.5"/>
        <path d="M48 34 v14" stroke="#B08968" stroke-width="1.7"/>
        <path d="M42 38h4M42 41h4M50 38h4M50 41h4" stroke="#C9B79C" stroke-width="1" stroke-linecap="round"/>
      </g>`;
    case 'write':
      return `<g class="cv-prop cv-prop--write">
        <path d="M56 32 L44 44 l-2 5 5-2 L59 35z" fill="#FFF1C9" stroke="#C79A4B" stroke-width="1.5" stroke-linejoin="round"/>
        <path d="M54 34 l3 3" stroke="#C79A4B" stroke-width="1.7"/>
      </g>`;
    case 'edit':
      return `<g class="cv-prop cv-prop--edit">
        <rect x="46" y="30" width="12" height="7" rx="2" fill="#8D99AE" stroke="#3D4453" stroke-width="1.5"/>
        <rect x="50" y="36" width="3" height="14" rx="1.5" fill="#B08968" stroke="#7A5C46" stroke-width="1.7"/>
      </g>`;
    case 'search':
      return `<g class="cv-prop cv-prop--search">
        <circle cx="49" cy="37" r="7" fill="#CFF3FF" fill-opacity=".7" stroke="#4A7FA5" stroke-width="2"/>
        <path d="M54 42 l5 5" stroke="#4A7FA5" stroke-width="2.5" stroke-linecap="round"/>
      </g>`;
    case 'web':
      return `<g class="cv-prop cv-prop--web">
        <path d="M42 46 L58 32" stroke="#5B5F97" stroke-width="5" stroke-linecap="round"/>
        <circle cx="59" cy="31" r="3.4" fill="#9BE3F2" stroke="#3D4172" stroke-width="1.9"/>
      </g>`;
    case 'plan':
      return `<g class="cv-prop cv-prop--plan">
        <rect x="42" y="32" width="15" height="18" rx="2" fill="#FFFDF5" stroke="#9AA0B5" stroke-width="1.5"/>
        <rect x="46" y="29.5" width="7" height="4" rx="1.5" fill="#C0C5D8"/>
        <path d="M45 39l2 2 4-4" stroke="#4FB5A5" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M45 45h9" stroke="#C7CBDA" stroke-width="1.5" stroke-linecap="round"/>
      </g>`;
    case 'summon':
      return `<g class="cv-prop cv-prop--summon">
        <path d="M41 42 q9-10 18-9 -2 9-8 12z" fill="#FFC857" stroke="#B98A2B" stroke-width="1.5" stroke-linejoin="round"/>
      </g>`;
    case 'ask':
      return `<g class="cv-prop cv-prop--ask">
        <circle cx="50" cy="38" r="9" fill="#FFF" stroke="#7C5CFF" stroke-width="1.8"/>
        <path d="M47 35a3 3 0 1 1 3 3v2" stroke="#7C5CFF" stroke-width="2" fill="none" stroke-linecap="round"/>
        <circle cx="50" cy="43.5" r="1.2" fill="#7C5CFF"/>
      </g>`;
    case 'mcp':
      return `<g class="cv-prop cv-prop--mcp">
        <rect x="44" y="34" width="11" height="12" rx="2.5" fill="#B8E1FF" stroke="#3E6D99" stroke-width="1.5"/>
        <path d="M47 34v-4M52 34v-4" stroke="#3E6D99" stroke-width="2" stroke-linecap="round"/>
        <path d="M49.5 46v3" stroke="#3E6D99" stroke-width="2" stroke-linecap="round"/>
      </g>`;
    default:
      return `<g class="cv-prop cv-prop--other">
        <circle cx="50" cy="39" r="6.5" fill="#D8DEE9" stroke="#6B7280" stroke-width="2.1"/>
        <circle cx="50" cy="39" r="2.2" fill="#6B7280"/>
        <path d="M50 30.5v3M50 44.5v3M41.5 39h3M55.5 39h3" stroke="#6B7280" stroke-width="1.8" stroke-linecap="round"/>
      </g>`;
  }
}

/* ------------------------------------------------------------------ *
 * Head silhouettes — the only per-species geometry.
 * ------------------------------------------------------------------ */

function headExtras(kind, p) {
  switch (kind) {
    case 'owl':
      return `
        <path d="M20 17 l4 7 -8 0z" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7"/>
        <path d="M44 17 l-4 7 8 0z" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7"/>
        <circle cx="26" cy="28" r="6.5" fill="#FFF8E7" stroke="${p.shade}" stroke-width="1.7"/>
        <circle cx="38" cy="28" r="6.5" fill="#FFF8E7" stroke="${p.shade}" stroke-width="1.7"/>
        <path d="M32 31 l-2.5 3 h5z" fill="${p.accent}"/>`;
    case 'fox':
      return `
        <path d="M17 20 l3 -9 7 5z" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7" stroke-linejoin="round"/>
        <path d="M47 20 l-3 -9 -7 5z" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7" stroke-linejoin="round"/>
        <path d="M24 31 q8 7 16 0 q-8 4 -16 0z" fill="#FFF3E6"/>`;
    case 'panda':
      return `
        <circle cx="19" cy="17" r="5.5" fill="${p.accent}"/>
        <circle cx="45" cy="17" r="5.5" fill="${p.accent}"/>
        <ellipse cx="25" cy="28" rx="5.5" ry="6.5" fill="${p.accent}" transform="rotate(-12 25 28)"/>
        <ellipse cx="39" cy="28" rx="5.5" ry="6.5" fill="${p.accent}" transform="rotate(12 39 28)"/>`;
    case 'beaver':
      return `
        <ellipse cx="32" cy="34" rx="8" ry="5" fill="#F2C9A0" stroke="${p.shade}" stroke-width="1.7"/>
        <rect x="29.5" y="35" width="5" height="5" rx="1" fill="#FFFDF5" stroke="${p.shade}" stroke-width="1"/>
        <path d="M32 35v5" stroke="${p.shade}" stroke-width=".8"/>`;
    case 'mouse':
      return `
        <circle cx="20" cy="18" r="7" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7"/>
        <circle cx="44" cy="18" r="7" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7"/>
        <circle cx="20" cy="18" r="4" fill="#FFD9E0"/>
        <circle cx="44" cy="18" r="4" fill="#FFD9E0"/>
        <circle cx="32" cy="33" r="2" fill="${p.accent}"/>`;
    case 'cat':
      return `
        <path d="M18 19 l1 -8 7 4z" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7" stroke-linejoin="round"/>
        <path d="M46 19 l-1 -8 -7 4z" fill="${p.body}" stroke="${p.shade}" stroke-width="1.7" stroke-linejoin="round"/>
        <path d="M29 32 q3 3 6 0" stroke="${p.accent}" stroke-width="1.9" fill="none" stroke-linecap="round"/>`;
    case 'robot':
    default:
      return `
        <rect x="14" y="24" width="4" height="8" rx="2" fill="${p.shade}"/>
        <rect x="46" y="24" width="4" height="8" rx="2" fill="${p.shade}"/>`;
  }
}

/* ------------------------------------------------------------------ *
 * The character itself
 * ------------------------------------------------------------------ */

/**
 * Build one villager as an SVG string.
 *
 * @param {object} opts
 * @param {string} opts.species  key into SPECIES
 * @param {string} [opts.toolCategory] prop to hold, omitted when not working
 * @param {boolean} [opts.showShadow]
 */
export function characterSvg({ species = 'villager', toolCategory = null, showShadow = true } = {}) {
  const p = SPECIES[species] || SPECIES.villager;
  const crown = p.crown
    ? `<path class="cv-crown" d="M22 12 l4 5 6-7 6 7 4-5 -2 9 H24z" fill="${p.trim}" stroke="#C79A2B" stroke-width="1.7" stroke-linejoin="round"/>`
    : '';
  const antenna = p.antenna
    ? `<g class="cv-antenna"><path d="M32 10 v-5" stroke="${p.shade}" stroke-width="1.8" stroke-linecap="round"/>
       <circle cx="32" cy="4" r="2.6" fill="${p.accent}"/></g>`
    : '';

  return `<svg class="cv-char cv-char--${species}" viewBox="0 0 64 64" xmlns="${SVG_NS}" aria-hidden="true">
  ${showShadow ? '<ellipse class="cv-shadow" cx="32" cy="59" rx="13" ry="3.4" fill="#000" opacity=".18"/>' : ''}
  <g class="cv-bob">
    <g class="cv-body">
      <path d="M22 40 h20 a7 7 0 0 1 7 7 v6 a3 3 0 0 1 -3 3 H18 a3 3 0 0 1 -3 -3 v-6 a7 7 0 0 1 7 -7z"
            fill="${p.body}" stroke="${p.shade}" stroke-width="2.1" stroke-linejoin="round"/>
      <path d="M26 47 h12 v9 H26z" fill="${p.accent}" opacity=".22"/>
    </g>
    <g class="cv-arm cv-arm--left">
      <rect x="11" y="41" width="6.5" height="13" rx="3.2" fill="${p.body}" stroke="${p.shade}" stroke-width="1.9"/>
    </g>
    <g class="cv-arm cv-arm--right">
      <rect x="46.5" y="41" width="6.5" height="13" rx="3.2" fill="${p.body}" stroke="${p.shade}" stroke-width="1.9"/>
    </g>
    <g class="cv-head">
      <rect x="16" y="12" width="32" height="26" rx="11" fill="${p.body}" stroke="${p.shade}" stroke-width="2.1"/>
      ${headExtras(p.head, p)}
      <g class="cv-face">
        <g class="cv-eyes">
          <circle class="cv-eye" cx="26" cy="27" r="2.8" fill="#23243A"/>
          <circle class="cv-eye" cx="38" cy="27" r="2.8" fill="#23243A"/>
          <circle cx="27" cy="26" r="1" fill="#FFF"/>
          <circle cx="39" cy="26" r="1" fill="#FFF"/>
        </g>
        <circle class="cv-blush" cx="21" cy="31" r="2.6" fill="#FF9EB5" opacity=".55"/>
        <circle class="cv-blush" cx="43" cy="31" r="2.6" fill="#FF9EB5" opacity=".55"/>
      </g>
      ${crown}
      ${antenna}
    </g>
    ${toolCategory ? propSvg(toolCategory) : ''}
  </g>
</svg>`;
}

/** A tiny badge used in lists and the legend. */
export function avatarSvg(species, size = 28) {
  const p = SPECIES[species] || SPECIES.villager;
  return `<svg class="cv-avatar" width="${size}" height="${size}" viewBox="0 0 64 64" xmlns="${SVG_NS}" aria-hidden="true">
    <circle cx="32" cy="32" r="30" fill="${p.accent}" opacity=".16"/>
    <g transform="translate(0,6) scale(0.92)" transform-origin="32 32">
      <rect x="16" y="12" width="32" height="26" rx="11" fill="${p.body}" stroke="${p.shade}" stroke-width="2.1"/>
      ${headExtras(p.head, p)}
      <circle cx="26" cy="27" r="2.8" fill="#23243A"/>
      <circle cx="38" cy="27" r="2.8" fill="#23243A"/>
    </g>
  </svg>`;
}

export function speciesLabel(species) {
  return (SPECIES[species] || SPECIES.villager).label;
}
