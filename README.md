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
unaffected — but no toasts are delivered. If Agent Office sees herdr reject a
toast because delivery is off, it says so on its status line rather than
retrying forever.

## Configuration

Agent Office is **zero-config**: with no file at all every setting below falls
back to its default. To change something, create `config.toml` in the plugin's
config directory (herdr passes it as `HERDR_PLUGIN_CONFIG_DIR`; typically
`~/.config/herdr/plugins/agent-office/`). Settings are read once at startup —
reopen the office pane to apply changes.

```toml
[office]
filter = "agents"            # "agents" (only panes with a detected agent) | "all"
renderer = "auto"            # "auto" | "unicode" (tier 1) | "ascii" (tier 0)
fps = 2                      # animation ticks per second, 1..10
theme = "default"            # sprite palette (only "default" so far)
name_template = "{name}"     # "{name}" | "{name:last-segment}"

[escalation]
blocked_threshold_s = 90     # blocked for this long -> toast
renotify_interval_s = 300    # remind every N seconds while still blocked; 0 = never
sound = "request"            # "request" | "done" | "none"
notify_done = false          # also toast when an agent finishes

[include]                    # optional narrowing; empty = the whole fleet
workspaces = []              # globs matched against the workspace label
exclude_agents = []          # e.g. ["codex"]
```

Check what your file actually parses to:

```sh
python3 -m office config-check
```

A malformed file never stops the office from opening: bad values fall back to
their defaults and the reason is listed by `config-check` and on the pane's
status line.

**`name_template`** shortens long names on desk nameplates and room labels.
`"{name:last-segment}"` keeps only the part after the last `/`, which turns a
label like `claude-org/8f3a…/g7/project:agent-office/a2` into `a2`.

### Escalation behaviour

A desk that stays `blocked` past `blocked_threshold_s` raises a toast, and its
speech bubble turns from `!` to a red `!!` on screen. Details worth knowing:

- **Agents that block together share one toast.** After the first desk crosses
  the threshold there is a 5 second collection window, so three stuck agents
  produce `✋ 3 agents are waiting`, not three separate toasts.
- **Reminders continue** every `renotify_interval_s` while the agent is still
  blocked, labelled `2nd reminder`, `3rd reminder`, and so on.
- **Unblocking resets everything** — the next block starts a fresh countdown.
- **Reopening the office pane does not reset the clock.** An agent already
  blocked at startup keeps the wait time recorded in `state.json`, so a restart
  does not hand a stuck agent another 90 seconds of silence. After a long
  outage (5 minutes without the office running) the countdown does start fresh
  — by then the agent may have unblocked and blocked again unobserved.
- **`s` mutes escalation** for the session; the desks still show raised hands.
- If herdr rate-limits a toast, Agent Office backs off 30 seconds and retries.

### Runtime state

The office pane writes `HERDR_PLUGIN_STATE_DIR/state.json` every 10 seconds and
whenever the desks change. It is what lets `agent-office.jump-blocked` pick the
genuinely longest-blocked agent and `agent-office.open` focus the office pane
that is already running.

Both actions still work without it: they only trust the file while an office
process is actively writing it, and otherwise fall back to ordering by pane id
and to opening a new pane. Deleting the file is harmless.

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

- `agent-office.open` — focus the running office pane, or open one.
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
