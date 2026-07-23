# Agent Office

A [herdr](https://herdr.dev) plugin that draws your agent fleet as a **pixel-art
office**: every herdr pane running an agent becomes a character at a desk that
animates by status (idle / working / blocked / done). Blocked agents raise a
hand; one key jumps you straight to them.

**What makes it different:** existing agent-visualizers detect state on the
*client* side — per-agent hooks or transcript-file parsing that each new agent
requires custom integration work. Agent Office reads state from herdr's native
`pane.agent_status_changed`, so it works with *every* agent herdr detects
(claude / codex / gemini / cursor / …) with zero setup, keeps working over
`herdr --remote`, and lives in the terminal where you already are — from
noticing a blocked agent to jumping to its pane there is no context switch.

Design is in [`docs/design.md`](docs/design.md) (source of truth).

## Quick Start

Agent Office is pure Python stdlib — no build step. It needs herdr >= 0.7.4 and
`python3` on PATH.

**Install (marketplace publication pending):** once published under the
`herdr-plugin` topic, install directly from GitHub:

```sh
herdr plugin install suisya-systems/agent-office
```

**Develop against a local checkout:**

```sh
herdr plugin link /path/to/agent-office
herdr plugin pane open --plugin agent-office --entrypoint office --placement tab
```

### Required: enable toast delivery

Escalation toasts (a blocked agent that stays stuck) use herdr's
`notification.show`, and herdr ships with toast delivery **off by default**. To
receive escalations, set this in your herdr config:

```toml
[ui.toast]
delivery = "herdr"
```

Without it the office view still works — desks, hand-raising, and jump are
unaffected — but no toasts are delivered.

## Keys (when the office pane is focused)

| key | action |
|---|---|
| arrows / `hjkl` | move the desk selection cursor |
| `Enter` | focus the selected agent's pane (jump) |
| `b` | jump to the longest-blocked agent |
| `Tab` | cycle through blocked agents |
| `a` | toggle filter (agents / all) |
| `s` | toggle escalation mute |
| `?` | help overlay |
| `q` | close the office pane |

## States and how they look

herdr's `AgentStatus` is the only input; the office adds a couple of
lifecycle/overlay states on top.

| state | from | look (tier 1) | animation |
|---|---|---|---|
| `IDLE` | `idle` | leaning back, coffee cup | steam wavers |
| `WORKING` | `working` | hunched over keyboard, monitor lit green | hands type |
| `BLOCKED` | `blocked` | **hand raised** + `!` speech bubble overhead | bubble blinks |
| `DONE` | `done` | stretching, green checkmark overhead | check pulses |
| `UNKNOWN` | `unknown` | grey silhouette, monitor off | none |
| `EMPTY` | pane gone / filtered out | desk with dark monitor only | none |

Overlays are drawn on top of the state: **FOCUSED** (brightened floor for the
pane focused in herdr), **SELECTED** (accent-colored desk frame under the office
cursor), and **ESCALATED** (a blocked desk past its threshold — the bubble turns
from `!` to a red `!!`, in sync with the toast). See
[`docs/character-states.md`](docs/character-states.md) for the full spec.

## Actions and keybindings

Two actions are exposed globally and work even when the office pane is not open:

- `agent-office.open` — open the office pane, or focus it if already open.
- `agent-office.jump-blocked` — focus the longest-blocked agent's pane.

Bind them to a herdr key so you can jump to a stuck agent from anywhere:

```toml
[[keys.command]]
key = "prefix+j"
run = "herdr plugin action invoke agent-office.jump-blocked"
```

herdr's built-in `open_notification_target` (`prefix+o`) also jumps to the pane
that raised the most recent toast.

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE) © 2026 Suisya Systems. Sprite grids are original.
