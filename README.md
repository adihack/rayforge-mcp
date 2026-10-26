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
| `get_document` | Machine (with active WCS), layers, steps (power/speed/passes, warnings), workpieces (mm) | – |
| `list_step_types`, `describe_step_settings` | Available operations; each setting with range/choices/units | – |
| `import_svg`, `import_file` | Add SVG markup or a file (SVG/DXF/PDF/PNG…), optional name, x/y/size; `split_by_color` puts each colour on its own layer | – |
| `set_workpiece`, `remove_workpieces` | Move / resize / rotate / move to another layer / delete | – |
| `add_layer`, `set_active_layer`, `update_layer`, `remove_layer` | Create, rename, reorder, show/hide, enable/disable, delete layers | – |
| `add_step`, `remove_step` | Workflow setup; new steps start within the machine's speed limits | – |
| `set_step_settings` | e.g. `{"power": 0.6, "cut_speed": 800, "passes": 2, "enabled": false}`, one undo step per call | – |
| `estimate_job` | Time (total and per layer), distances, bounding boxes, Rayforge sanity-check warnings | – |
| `preview_toolpath` | PNG of the generated toolpath on the work area | – |
| `get_gcode`, `save_project` | Machine code head / save to file; save `.ryp` | – |
| `new_document`, `open_project` | Start an empty document / open a `.ryp`; unsaved changes are never dropped without asking in Rayforge | – |
| `undo`, `redo` | Rayforge's undo stack; report what changed and what's next | – |
| `machine_info` | Machine setup plus the controller's own settings (GRBL `$$`), with checks such as laser mode `$32` and max power `$30` | never |
| `machine_status`, `machine_pause`, `machine_stop` | State/position/progress; feed hold; cancel | never |
| `machine_start_job`, `machine_frame`, `machine_home`, `machine_jog`, `machine_resume`, `machine_clear_alarm` | Moves the head or fires the laser | **Allow in Rayforge** |
| `machine_send_command` | One console line; returns the reply (`ok` / `error:N` with its meaning) | **Allow in Rayforge**, except read-only queries (`$$`, `$#`, `$G`, `$I`, `$N`, `$`) |
| `set_controller_setting` | Writes `$N=value` while idle, then reads `$$` back to confirm | **Allow in Rayforge** |
| `set_machine_settings` | Rayforge's machine profile: max cut/travel speed, acceleration, jog speed, frame speed/power, laser max S value | **Allow in Rayforge** |

Design edits use Rayforge's own undoable commands, so they show up live
and `Ctrl+Z` works.

Rayforge burns every layer, including hidden ones: hiding a layer only
affects the canvas. To frame or run only some layers, disable the
others with `update_layer(enabled=false)`, which turns their steps off.

Speeds above the machine's maximum are refused. New steps, including
the default steps Rayforge adds to a layer on import, are brought
within the machine's limits.

A workpiece's `x`, `y`, `width` and `height` describe the geometry the
laser follows. Rayforge pads imported SVGs with a small margin, so the
X/Y fields in its UI show a slightly larger box; tools report that box
as `frame`. Placing a 60×40 mm design at x=20 puts the cut at x=20.

## Safety model

- Anything that can move the head or fire the laser opens a dialog in
  Rayforge. **Deny** is the default. Unanswered requests are denied
  after the timeout, and nothing is sent. The dialog text is built from
  the actual job (estimated time, burn area, per-step power/speed/passes,
  sanity-check warnings), never from text the AI wrote.
- One approval at a time. `machine_stop`, `machine_pause` and
  `machine_status` never wait, even while a dialog is open.
- Motion commands are refused while the controller is busy (homing,
  running, jogging); homing is also allowed from ALARM.
- Console lines, controller settings and machine-profile changes also
  need Allow. The dialog shows the following:
  - for a console line: the exact line, and what it does (moves the
    head, turns the laser on, changes a setting…)
  - for a setting or profile change: the current and the new values
- Only one line is sent per console call, and real-time characters are
  refused.
- The frame and job dialogs list the steps that will run and say
  where the head goes at the end (Rayforge's `G0 X0 Y0` in the
  active work coordinate system).
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
- `dev/smoke_test.py` runs MCP checks over HTTP against a running
  Rayforge: design tools plus the safe refusals. It edits the document,
  so it refuses to run unless the document is empty. Point it at a
  test instance (separate `--config` and port), never at your working
  Rayforge. It needs no token and no laser connected.
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
  settings page and server all work without a restart.
- **Upgrades without restart (12 checks):** v0.2.0 → v0.3.0 → v0.3.1
  installed over each other at runtime. v0.3.1 stopped the leftover
  v0.2.0 server and took over the port in 0.6 s; reinstalling v0.3.1
  handed over cleanly; after uninstall the port closed on its own in
  0.4 s.
- **Design tools (smoke test, 56 checks):** G-code carried the set power
  and speed; passes scaled cut length exactly; imported geometry lands
  exactly at the requested x/y and size; one undo reverts a whole
  settings call. Since 0.4.0 it also covers the following:
  - choice settings by label or name
  - speed limits
  - colour split
  - layer rename, reorder, hide and disable (one undo step)
  - per-layer times that add up to the total
  - moving workpieces between layers
  - save, new and open document
- **Machine flow** against a fake GRBL 1.1 controller (Rayforge's
  GRBL-telnet driver). A denied job streamed nothing. An approved job
  streamed `M4 S350`. Pause/resume/stop sent `!`/`~`/soft reset, then
  `M5`. Jog sent `$J=…` and home sent `$H`. A timed-out request sent
  nothing, and stop/status answered instantly while a dialog was open.
  `machine_info` flagged `$32=0` and a `$30` mismatch, and reported no
  checks for a correctly configured controller. With a controller that
  takes 15 s to home, Rayforge gave up after 10 s, and `machine_home`
  followed the controller and reported success after 15.5 s. With a
  12 s limit it reported "still homing", and frame and jog were refused
  until the controller was idle.
- **Console and settings tools (22 checks)** against the fake GRBL:
  - `$$` and `$G` ran without a dialog. A denied `G0` sent nothing.
  - An approved bad command came back as `error:3` with its meaning.
    Real-time characters and multi-line input were refused.
  - `$110` went 3000 → 1000 and was confirmed by reading `$$` back.
    Writing the same value again opened no dialog. A denied `$111`
    stayed unchanged.
  - After the change, `machine_info` warned that Rayforge's speeds were
    above `$110`/`$111`.
  - `set_machine_settings` changed max speeds and frame power/speed
    after Allow. Rayforge saved them in the machine file, and steps
    above the new limits were listed.
- **Real hardware (0.3.1):** TwoTrees TTC-H40 (grblHAL, 20 W diode).
  Tested `machine_info`, home, jog, frame, and a 10–100 % power-test
  job.
- **Snap Rayforge 1.13.1** (strict confinement): installed with Install
  from URL and used for a design-only session from Claude Code.

**Not tested yet:** Windows/macOS, Flatpak, raster-image imports; the
0.4.0 changes on real hardware.

## Changelog

- **0.4.0**
  - Fixed: choice settings such as `depth_mode` and `scan_mode`
    rejected both their name (`CONSTANT_POWER`) and their label
    (`Constant Power`). Both now work, in any case.
  - Speeds above the machine's maximum are refused. New steps,
    including Rayforge's default steps on import, start within the
    machine's limits; they used to start at 5000 mm/min travel.
  - New tools:
    - `update_layer`: rename, reorder, show/hide, enable/disable
    - `remove_layer`
    - `new_document`
    - `open_project`
  - `set_step_settings` takes `enabled`. `set_workpiece` can move
    workpieces to another layer.
  - `import_svg`/`import_file` take `split_by_color`: one layer per
    colour, placed as one design.
  - `estimate_job` gives the time per layer, split by operation.
  - `get_document`/`machine_info` report the active work coordinate
    system, its offset, and where the head goes after a job.
  - `machine_home` waits until homing has finished and reports how it
    ended. If Rayforge stops waiting early (its limit is 10 s), the
    tool keeps watching the controller.
  - Motion commands are refused while the controller is busy.
  - New controller tools (each needs Allow):
    - `machine_send_command`: one console line with its reply;
      read-only queries need no approval
    - `set_controller_setting`: `$N=value`, confirmed by reading `$$`
      back
    - `set_machine_settings`: Rayforge's machine profile
  - `machine_info` warns when Rayforge's max speeds are above the
    controller's `$110`/`$111`, and shows jog and frame speed.
  - The approval dialogs now do the following:
    - list steps on hidden layers, which still burn
    - say where the head goes at the end
  - Offset Fill steps carry a warning. On some shapes (e.g. outlined
    text at a 0.08 mm interval) Rayforge 1.13's ring computation never
    ends and freezes the app. The bridge cannot interrupt it.

- **0.3.1**
  - Upgrading or reinstalling no longer leaves the old version's server
    holding the port. Rayforge loads the new copy without unloading the
    old one, so the new copy now stops any earlier copy's server before
    starting (including servers left by 0.2.0 and 0.3.0), and a server
    whose copy was replaced or uninstalled stops itself within a second.
- **0.3.0**
  - New read-only `machine_info` tool: machine setup plus the
    controller's settings, with checks for GRBL laser mode (`$32`), max
    power (`$30`) and travel (`$130`/`$131`).
  - Positions and sizes now refer to the cut geometry, compensating for
    Rayforge's SVG import margin. Resizing or rotating keeps the
    geometry's corner in place unless x/y are given.
  - `import_svg` uses the `name` argument as the workpiece name.
  - `set_step_settings` is one undo step per call; `undo`/`redo` report
    what they changed and what comes next.
  - `describe_step_settings` describes power as a 0–1 fraction and gives
    speed units and the machine's limits.
  - Machines with no driver no longer show as connected; G-code line
    counts match between `estimate_job` and `get_gcode`.
  - The smoke test refuses to run against a document that has content.
- **0.2.0**: first release.

## Known limitations

- `machine_frame` calls a private main-window method
  (`_start_frame_job`), because Rayforge's Frame action opens a popover.
  A Rayforge update could break it.
- `machine_jog` uses machine axis directions. On machines with reversed
  axes, "+X" may differ from the arrow in the UI.
- Offset Fill can freeze Rayforge on some shapes. The computation runs
  inside Rayforge, so the bridge cannot time it out; avoid Offset Fill
  for text and detailed shapes, or use a line interval of 0.2 mm or
  more.
- `new_document` uses a private Rayforge method
  (`ProjectCmd._do_create_new_project`).
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
  - Uninstalling and upgrading do not call `on_unload`; they drop the
    old copy's modules and load the new copy. The server checks twice a
    second whether its copy is still the loaded one and stops if not,
    and a new copy stops any earlier copy's server before it starts.
