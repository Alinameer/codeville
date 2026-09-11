#!/usr/bin/env bash
#
# Regenerate Codeville's pixel art with the Codex CLI bundled in the ChatGPT
# desktop app, then normalise each result into a game-ready sprite sheet.
#
#   ./scripts/generate_art.sh            # everything that is missing
#   ./scripts/generate_art.sh mayor fox  # only the named assets
#   FORCE=1 ./scripts/generate_art.sh    # redo even if the file exists
#
# The committed PNGs in web/assets/sprites/ are the output of this script, so
# contributors do not need a ChatGPT subscription to run Codeville — only to
# change the art.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX="/usr/lib/chatgpt/resources/codex"
OUT="$HERE/web/assets/sprites"
RAW="$HERE/.art-raw"
JOBS="${JOBS:-3}"

mkdir -p "$OUT" "$RAW"

[ -x "$CODEX" ] || {
  echo "Codex CLI not found at $CODEX"
  echo "It ships inside the ChatGPT desktop app. The committed sprites in"
  echo "web/assets/sprites/ are what the app actually loads — you only need"
  echo "this script to change the art."
  exit 1
}

# Every sprite shares this so the whole cast reads as one set of artwork.
STYLE="16-bit SNES-style pixel art. Crisp hard-edged pixels, limited palette, \
1px dark brown outline, no anti-aliasing, no blur, no gradients, no dithering. \
Fully TRANSPARENT background with preserved alpha - no checkerboard, no backdrop, \
no scenery, no ground, no cast shadow, no text, no labels, no frame borders, \
no grid lines."

WALK="Layout: one horizontal strip of exactly 4 evenly spaced frames on a 256x64 \
canvas, each frame 64x64. The 4 frames are a side-view WALK CYCLE facing RIGHT: \
contact, passing, contact opposite, passing. The same character at the SAME SIZE \
and SAME EYE HEIGHT in every frame, centred in its cell, feet on the cell bottom. \
Chibi proportions: big head, small body, two stubby legs."

PROP="Layout: a single object centred on a 64x64 canvas, drawn in three-quarter \
view, resting on nothing."

# Backdrops are the one asset that is NOT transparent — they are the ground the
# villagers walk on, so they need a sky, a horizon and a flat strip at the bottom.
SCENE="Layout: a single 320x120 side-on landscape, one frame, no character in it. \
The bottom third is FLAT LEVEL GROUND for characters to walk along, unobstructed \
across the full width. Buildings and scenery sit in the upper two thirds and along \
the back edge only. Bright and inviting, gentle depth with two parallax layers. \
16-bit SNES-style pixel art, crisp hard-edged pixels, limited palette, no \
anti-aliasing, no blur, no text, no labels, no characters, no creatures, no people, \
no UI, no frame border."

# name|frames|prompt
read -r -d '' ASSETS <<EOF || true
mayor|4|a cute chibi robot king with a small golden crown, cream-white enamel body, one glowing cyan visor eye and one golden ring eye, short stubby arms
worker|4|a cute chibi robot worker, pale mint-green rounded body, a single small leaf sprig on its head, wearing a tiny orange work tabard, big friendly dark eyes
explorer|4|a cute chibi fennec fox scout, tawny orange fur, oversized swept-back ears, aviator goggles pushed up on its forehead, small satchel on its hip
planner|4|a cute chibi beaver architect, warm brown fur, flat paddle tail, wearing a carpenter's apron, a rolled blueprint tucked under one arm
reviewer|4|a cute chibi panda inspector, white and black fur, small round spectacles, holding a magnifying glass, tidy dark waistcoat
generalist|4|a cute chibi ginger tabby cat handyman, orange striped fur, blue denim dungarees with a roomy front pocket, a small wrench in one paw
guide|4|a cute chibi owl guide, soft lilac feathers, large amber eyes, carrying a small glowing lantern in one wing
tinkerer|4|a cute chibi grey mouse tinkerer, round ears, tiny goggles, an oversized brass dial strapped to its back
villager|4|a cute chibi hooded blob creature, soft dusty-pink cloak, no visible hands or feet, two simple dark dot eyes peeking from the hood
station_run|1|a small dark terminal computer on a low wooden desk, glowing green text on its screen
station_edit|1|a blacksmith anvil with a hammer resting on it, small orange sparks
station_read|1|a short wooden bookshelf packed with colourful books, one book open on top
station_write|1|a wooden writing desk with an open scroll, an inkpot and a quill
station_search|1|a wooden lookout post with a large magnifying glass mounted on it
station_web|1|a brass telescope on a wooden tripod, pointed up and to the right
scene_meadow|0|a sunny rolling green meadow village with two small thatched cottages set back from a flat dirt path, wildflowers, a pale blue sky with soft clouds
scene_forest|0|a peaceful pine forest clearing with tall conifers at the back, mossy stumps and ferns, warm shafts of light, a flat mossy path along the bottom
scene_harbor|0|a calm seaside harbour with a wooden jetty and two small moored sailing boats at the back, gulls, a flat plank boardwalk along the bottom
scene_canyon|0|a warm desert canyon with layered orange rock walls and tall mesas at the back, a few cacti, a flat sandy floor along the bottom
scene_citadel|0|a tidy stone citadel courtyard with pale towers and banners at the back, a flat flagstone floor along the bottom, a violet dusk sky
EOF

want=("$@")

generate() {
  local name="$1" frames="$2" subject="$3"
  local final="$OUT/$name.png"
  local raw="$RAW/$name.raw.png"

  if [ -f "$final" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "  = $name (already present)"
    return 0
  fi

  local layout="$WALK"
  [ "$frames" = "1" ] && layout="$PROP"
  [ "$frames" = "0" ] && layout="$SCENE"

  # A backdrop is opaque scenery and carries its own style text; giving it the
  # shared STYLE would also demand a transparent background, and that alpha shows
  # up as black bands once the image is flattened.
  local style="$STYLE"
  [ "$frames" = "0" ] && style=""

  local prompt="Use your built-in image_gen tool to generate: ${subject}. ${layout} ${style} \
Intended use: art for a desktop game UI. Save as ${raw}. Reply only with the final file path."

  echo "  > $name generating..."
  if ! timeout 600 "$CODEX" exec --approve-for-me --skip-git-repo-check "$prompt" >"$RAW/$name.log" 2>&1; then
    echo "  ! $name FAILED (see $RAW/$name.log)"
    return 1
  fi

  # Codex sometimes reports success while writing only to its own store.
  if [ ! -f "$raw" ]; then
    local fallback
    fallback="$(find "$HOME/.codex/generated_images" -name '*.png' -newermt '-11 minutes' \
                 -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
    [ -n "$fallback" ] && cp "$fallback" "$raw"
  fi
  [ -f "$raw" ] || { echo "  ! $name produced no image"; return 1; }

  if [ "$frames" = "0" ]; then
    # A backdrop is one opaque image, not a strip — just fit it to the stage.
    python3 - "$raw" "$final" <<'PYEOF' || { echo "  ! $name could not be resized"; return 1; }
import sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
im = Image.open(src).convert("RGBA")
# Flatten over sky blue, not black: any alpha the model left behind would
# otherwise become hard black bands across the top of the scene.
flat = Image.new("RGBA", im.size, (150, 205, 245, 255))
flat.alpha_composite(im)
flat.convert("RGB").resize((320, 120), Image.LANCZOS).quantize(colors=64).save(dst)
PYEOF
  else
    python3 "$HERE/scripts/make_sprite.py" "$raw" "$final" --frames "$frames" --cell 64 \
      || { echo "  ! $name could not be sliced"; return 1; }
  fi
  echo "  + $name done"
}

echo "Generating Codeville art (up to $JOBS at a time)"
running=0
while IFS='|' read -r name frames subject; do
  [ -z "$name" ] && continue
  if [ ${#want[@]} -gt 0 ]; then
    match=0
    for w in "${want[@]}"; do [ "$w" = "$name" ] && match=1; done
    [ $match -eq 1 ] || continue
  fi

  # </dev/null matters: a backgrounded job inherits this loop's stdin and will
  # happily consume the rest of the asset list, so the loop ends after one batch.
  generate "$name" "$frames" "$subject" </dev/null &
  running=$((running + 1))
  if [ "$running" -ge "$JOBS" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
done <<< "$ASSETS"
wait

echo
echo "Sprites in $OUT:"
ls -1 "$OUT" 2>/dev/null | sed 's/^/  /'
