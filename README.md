# MCP Bridge for Rayforge (experimental)

A [Rayforge](https://github.com/barebaric/rayforge) addon that lets AI
assistants (Claude Code, Claude Desktop, any MCP client) work with the
open document. They can import designs, set up layers and power/speed,
estimate and preview the toolpath, and, **only after you click Allow
in Rayforge**, run the laser.

It speaks the Model Context Protocol over HTTP on
`http://127.0.0.1:8765/mcp`. It only uses the Python standard library,
so nothing extra has to be installed into Rayforge's environment.

## Install

In Rayforge, open **Settings → Addons → Install New Addon… → Install
from URL…** and enter:

```text
https://github.com/adihack/rayforge-mcp
```

The server starts right away and shows a toast with its address. It
also starts with Rayforge from then on.

For development, clone the repository and link it into Rayforge's
addons directory instead, then restart Rayforge:

```bash
git clone https://github.com/adihack/rayforge-mcp.git
```

```bash
ln -s "$PWD/rayforge-mcp" ~/.config/rayforge/addons/
```

Windows: `%LOCALAPPDATA%\rayforge\rayforge\addons`. macOS:
`~/Library/Application Support/rayforge/addons`.

## Use

- **Tools → MCP Server for AI Assistants** turns the server on and off.
- **Settings → MCP Bridge** has these options:
  - **Run Server**, with live status (or why it could not start)
  - **Start With Rayforge**
  - **Port** (applied a moment after you stop changing it)
  - **Access Token**: optional; *Generate* makes one
  - **Allow Connections From Other Computers**: only works with a token
  - **Allow Machine Control**: off = design-only; status, pause and
    stop always remain
  - **Approval Timeout**
  - A ready-to-copy **Claude Code** command that includes the token
    header when one is set

Connect Claude Code (the settings page shows this command):

```bash
claude mcp add --transport http rayforge http://127.0.0.1:8765/mcp
```

Claude Desktop, via the `mcp-remote` stdio adapter, in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rayforge": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
    }
  }
}
```

Then ask, for example: *"Make a 60×40 mm name tag with rounded corners
and the text HELLO, cut at 80% / 400 mm/min, and show me the preview."*

## Tools

| Tool | What it does | Approval |
|---|---|---|
| `get_document` | Machine, layers, steps (power/speed/passes), workpieces (mm) | – |
| `list_step_types`, `describe_step_settings` | Available operations; each setting with range/choices | – |
| `import_svg`, `import_file` | Add SVG markup or a file (SVG/DXF/PDF/PNG…), optional x/y/size | – |
| `set_workpiece`, `remove_workpieces` | Move / resize / rotate / delete | – |
| `add_layer`, `set_active_layer`, `add_step`, `remove_step` | Layer and workflow setup | – |
| `set_step_settings` | e.g. `{"power": 0.6, "cut_speed": 800, "passes": 2}` | – |
| `estimate_job` | Time, distances, bounding boxes, Rayforge sanity-check warnings | – |
| `preview_toolpath` | PNG of the generated toolpath on the work area | – |
| `get_gcode`, `save_project` | Machine code head / save to file; save `.ryp` | – |
| `undo`, `redo` | Rayforge's undo stack | – |
| `machine_status`, `machine_pause`, `machine_stop` | State/position/progress; feed hold; cancel | never |
| `machine_start_job`, `machine_frame`, `machine_home`, `machine_jog`, `machine_resume`, `machine_clear_alarm` | Moves the head or fires the laser | **Allow in Rayforge** |

Design edits use Rayforge's own undoable commands, so they show up live
and `Ctrl+Z` works.

## Safety model

- Anything that can move the head or fire the laser opens a dialog in
  Rayforge. **Deny** is the default. Unanswered requests are denied
  after the timeout, and nothing is sent. The dialog text is built from
  the actual job (estimated time, burn area, per-step power/speed/passes,
  sanity-check warnings), never from text the AI wrote.
- One approval at a time. `machine_stop`, `machine_pause` and
  `machine_status` never wait, even while a dialog is open.
- Turning the server off (or disabling the addon) closes a pending
  dialog, and a late click on Allow does nothing.
- Starting a job goes through Rayforge's normal Send action, so its own
  sanity-check and pointer-alignment dialogs still apply.
- The server listens on `127.0.0.1` only, unless you allow other
  computers (requires a token). Requests from web pages on other
  origins are rejected.
- The settings, including the token, are stored in
  `<rayforge config>/addon_data/mcp_bridge/settings.yaml` with
  permissions `0600`.

## Development

- Releases are `vX.Y.Z` git tags; Rayforge and the
  [addon registry](https://github.com/barebaric/rayforge-registry) take
  the version from the tag. To release, bump `VERSION` in
  `mcp_bridge/__init__.py` (shown to MCP clients), commit, then
  `git tag vX.Y.Z && git push origin vX.Y.Z`.
- `dev/smoke_test.py` runs 22 MCP checks over HTTP against a running
  Rayforge: design tools plus the safe refusals. Run it with an empty
  document, no token, and no laser connected.
- `RAYFORGE_MCP_DEMO=1` treats Rayforge's "no device" placeholder driver
  as a machine, so you can try the approval dialogs without hardware.

Tested against Rayforge 1.13.1 (git main, 2026‑10‑26) on Linux,
headless:

- **Addon integration (36 checks):** auto-start from
  `main_window_ready`; Tools-menu toggle and settings switch stay in
  sync; live port/token/LAN/machine-control changes; disabling another
  addon leaves the server running; disabling this addon stops it, and
  re-enabling it at runtime starts it again with the window bound;
  settings persisted at `0600`.
- **Install from a git URL (13 checks)** through Rayforge's own
  installer: version taken from the `v0.2.0` tag; the menu toggle,
  settings page and server all work without a restart. After uninstall,
  the next request gets 503 and the server shuts down.
- **Design tools (22 smoke checks):** G-code carried the set power and
  speed, and passes scaled cut length exactly.
- **Machine flow** against a fake GRBL 1.1 controller (Rayforge's
  GRBL-telnet driver). A denied job streamed nothing. An approved job
  streamed `M4 S350`. Pause/resume/stop sent `!`/`~`/soft reset, then
  `M5`. Jog sent `$J=…` and home sent `$H`. A timed-out request sent
  nothing, and stop/status answered instantly while a dialog was open.

**Not tested yet:** a real laser, Windows/macOS, Snap/Flatpak builds,
raster-image imports.

## Known limitations

- `machine_frame` calls a private main-window method
  (`_start_frame_job`), because Rayforge's Frame action opens a popover.
  A Rayforge update could break it.
- `machine_jog` uses machine axis directions. On machines with reversed
  axes, "+X" may differ from the arrow in the UI.
- Only stateless HTTP is implemented. There are no server→client
  notifications, so clients poll `machine_status` for progress.
- Workarounds for Rayforge addon-lifecycle quirks:
  - Rayforge calls `on_unload` on every addon whenever *any* addon is
    disabled, so the bridge checks its own state before stopping.
  - `main_window_ready` does not run when an addon is enabled at
    runtime, so the bridge also starts from its action setup.
  - Installing at runtime runs neither `register_actions` nor
    `main_window_ready`, so on import the bridge attaches to an
    already-open window.
  - Uninstalling does not call `on_unload`, so the server checks on
    each request that the addon is still loaded and stops if not.
