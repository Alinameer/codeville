# Contributing to Codeville

Thanks for taking a look. Codeville is small and has no build step, so getting
started is quick.

## Running it

```bash
git clone https://github.com/Alinameer/codeville.git
cd codeville

cd daemon && python3 -m codeville --print-url   # headless, open the URL
./bin/codeville-tray                            # panel applet (needs GTK)
```

Nothing to install. The daemon is standard library only, and the UI is plain ES
modules served straight from `web/` — edit a file and reload the page.

## Tests

```bash
python3 -m unittest discover -s daemon/tests -v
```

They are standard library too, and they run in about six seconds. Please add
tests for behaviour you change. The suite already pins a few things deliberately:

- the RFC 6455 handshake, against the worked example in the spec;
- the server's security rules (loopback binding, token auth, traversal, origin);
- that no transcript record, however malformed, can make the parser raise.

That last one matters more than it looks. Codeville reads files another process
is actively writing, so it sees truncated lines and shapes that did not exist
when the code was written. A parser crash freezes the whole view.

## Things worth knowing before you change them

**Polling, not inotify.** Deliberate — see the README. Please do not swap it for
inotify without measuring against a real `~/.claude` first.

**Transcripts are huge.** The largest on the author's machine is 137 MB. Never
read one whole; `tailer.py` exists for this.

**Slugs are lossy.** `/`, `.` and `_` all flatten to `-`, so a project directory
name cannot be reversed into a path. Read `cwd` from the transcript instead.

**Codeville only reads.** It must never write to `~/.claude`, and nothing may
leave the machine. Please keep it that way.

**Animate transform and opacity only.** Thirty villagers move at once; anything
that triggers layout will show.

## Adding a character

Agent types map to species in `web/js/characters.js`. To add one:

1. Add a palette to `SPECIES` — body, shade, accent, trim, and a `head` silhouette.
2. If it needs a new silhouette, add a case to `headExtras()`. Keep it inside the
   64x64 viewBox and reuse the shared proportions so the cast stays a family.
3. Map the agent type in `AGENT_SPECIES`.

Unknown agent types fall back to `villager` through a fuzzy match, so nothing
breaks if you skip step 3 — the agent just gets a generic body.

## Reporting a bug

Please include your distro and desktop, `python3 --version`, whether you are on
X11 or Wayland, and anything the daemon printed. If it is a rendering problem, a
screenshot helps a lot.
