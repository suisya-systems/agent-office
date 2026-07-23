# Agent Office

A [herdr](https://herdr.dev) plugin that draws your agent fleet as a **pixel-art
office**: every herdr pane running an agent becomes a character at a desk that
animates by status (idle / working / blocked / done). Blocked agents raise a
hand; one key jumps you to them.

Design is in [`docs/design.md`](docs/design.md) (source of truth).

## Status: Stage 2 core

Implemented in this stage (docs/design.md section 14, item 1):

- **Subscriber** - lifecycle connection L + per-pane status connection S with the
  subscribe-then-snapshot startup, debounced re-subscribe, and reconnect backoff
  (design section 3).
- **OfficeState** - the pure, unit-tested state model (design section 4).
- **Renderer** - tier 1 (Unicode half-block pixel art) and tier 0 (ASCII
  fallback), reusing the Stage 1 mock sprites (design section 5).
- **Input + jump** - keyboard control and `pane.focus` (design section 6).
- **Manifest** - `herdr-plugin.toml` with the office pane and two actions.

Not yet (later stages, intentionally out of scope here): escalation /
`notification.show`, a config file (defaults are hard-coded), `state.json`,
Windows, and tier 2 (kitty graphics).

## Try it (dev)

```sh
herdr plugin link /path/to/agent-office
herdr plugin pane open --plugin agent-office --entrypoint office --placement tab
```

Requires herdr >= 0.7.4 and `python3` on PATH. Pure Python stdlib, so there is
no build step.

### Keys (when the office pane is focused)

| key | action |
|---|---|
| arrows / `hjkl` | move the desk cursor |
| `Enter` | focus the selected agent's pane (jump) |
| `b` | jump to the longest-blocked agent |
| `Tab` | cycle through blocked agents |
| `a` | toggle filter (agents / all) |
| `s` | toggle mute (escalation lands in a later stage) |
| `?` | help |
| `q` | close the office pane |

The `jump-blocked` action also works without the office pane open
(`herdr plugin action invoke agent-office.jump-blocked`).

## Tests

```sh
python3 -m unittest discover -s tests
```

## Notes from Stage 2

- **Verified**: `pane.updated` does **not** fire on `agent_status` changes (only
  `pane.agent_status_changed` does), so the per-pane status connection S is
  required and cannot be collapsed into connection L (design section 3).
- tier 0 still emits ANSI color (SGR); it drops only the Unicode half-blocks. A
  strictly `TERM=dumb` monochrome path is a later refinement.

## License

MIT (see design section 11). Sprite grids are original.
