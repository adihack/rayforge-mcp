"""
Smoke test for the Rayforge MCP bridge: talks MCP over HTTP like a
client would and exercises the design tools. Machine tools are only
checked for their safe refusals, so run it with no laser connected.
It edits the open document, so it refuses to run unless that document
is empty.

    python3 dev/smoke_test.py [http://127.0.0.1:8765/mcp] [preview.png]

If an access token is set, the test cannot connect; clear it first.
"""

import base64
import json
import os
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"
PREVIEW = sys.argv[2] if len(sys.argv) > 2 else "preview.png"
_next_id = 0

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="40mm"
     viewBox="0 0 60 40">
  <rect x="2" y="2" width="56" height="36" rx="4" fill="none" stroke="black"/>
  <circle cx="20" cy="20" r="10" fill="none" stroke="black"/>
  <path d="M35 30 L45 10 L55 30 Z" fill="none" stroke="black"/>
</svg>"""


def post(payload):
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read()
        return resp.status, json.loads(body) if body else None


def rpc(method, params=None):
    global _next_id
    _next_id += 1
    status, body = post(
        {
            "jsonrpc": "2.0",
            "id": _next_id,
            "method": method,
            "params": params or {},
        }
    )
    assert status == 200, status
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def call(tool_name, /, **arguments):
    result = rpc("tools/call", {"name": tool_name, "arguments": arguments})
    texts = [c["text"] for c in result["content"] if c["type"] == "text"]
    images = [c for c in result["content"] if c["type"] == "image"]
    data = None
    if texts and not result.get("isError"):
        try:
            data = json.loads(texts[0])
        except ValueError:
            data = texts[0]
    return result.get("isError", False), data, texts, images


def check(label, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}  {detail}")
    if not condition:
        check.failed += 1


check.failed = 0

init = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "0"},
    },
)
check(
    "initialize",
    init["serverInfo"]["name"] == "rayforge-mcp-bridge",
    init["protocolVersion"],
)
status, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"})
check("initialized notification -> 202", status == 202)

tools = [t["name"] for t in rpc("tools/list")["tools"]]
check("tools/list", "import_svg" in tools, f"{len(tools)} tools")

err, docinfo, _, _ = call("get_document")
existing = sum(len(layer["workpieces"]) for layer in docinfo["layers"])
if existing:
    sys.exit(
        f"Refusing to run: the document at {URL} already has {existing} "
        "workpiece(s), and this test edits it. Open an empty document "
        "(or use a test instance on another port) and try again."
    )
check(
    "get_document",
    not err and docinfo["layers"],
    f"{len(docinfo['layers'])} layers, machine {docinfo['machine']['name']!r}",
)
layer0 = docinfo["layers"][0]
print(
    "      layer 0 steps:",
    [
        (s["name"], s["settings"].get("power"), s["settings"].get("cut_speed"))
        for s in layer0["steps"]
    ],
)

err, types, _, _ = call("list_step_types")
check("list_step_types", not err and types, [t["type"] for t in types][:6])


def close(a, b, tol=0.05):
    return a is not None and abs(a - b) < tol


# The design's cut geometry spans x 2..58, y 2..38 (56 x 36 mm).
err, imp, texts, _ = call(
    "import_svg", svg=SVG, name="Smoke tag", x=20, y=30, layer=0
)
check("import_svg", not err and imp["imported"], texts[0][:300])
wp = imp["imported"][0] if not err else None
if wp:
    check(
        "import: cut geometry at x=20,y=30, 56x36 mm",
        close(wp["x"], 20)
        and close(wp["y"], 30)
        and close(wp["width"], 56)
        and close(wp["height"], 36),
        f"x={wp['x']} y={wp['y']} {wp['width']}x{wp['height']}, "
        f"frame {wp['frame']}",
    )
    check("import uses the name argument", wp["name"] == "Smoke tag")

err, settings, texts, _ = call("describe_step_settings", layer=0, step=0)
check(
    "describe_step_settings",
    not err,
    [s["key"] for s in settings["settings"]] if not err else texts,
)
if not err:
    by_key = {s["key"]: s for s in settings["settings"]}
    check(
        "power described as a fraction, speeds in mm/min",
        "fraction" in by_key["power"]["description"]
        and by_key["cut_speed"].get("unit") == "mm/min",
    )
    before = {k: by_key[k]["value"] for k in ("power", "cut_speed", "passes")}

err, step, texts, _ = call(
    "set_step_settings",
    layer=0,
    step=0,
    settings={"power": 0.42, "cut_speed": 777, "passes": 2},
)
check(
    "set_step_settings",
    not err
    and step["settings"]["power"] == 0.42
    and step["settings"]["cut_speed"] == 777
    and step["passes"] == 2,
    texts[0][:200],
)

err, undo, texts, _ = call("undo")
_e, doc_after, _t, _i = call("get_document")
s0 = doc_after["layers"][0]["steps"][0]
check(
    "one undo reverts the whole settings call",
    not err
    and s0["settings"]["power"] == before["power"]
    and s0["settings"]["cut_speed"] == before["cut_speed"]
    and s0["passes"] == before["passes"],
    texts[0][:160],
)
check(
    "undo says what it undid",
    not err and "power" in (undo.get("undone") or ""),
    undo.get("undone") if not err else "",
)
err, redo, texts, _ = call("redo")
_e, doc_after, _t, _i = call("get_document")
s0 = doc_after["layers"][0]["steps"][0]
check(
    "redo restores it and says so",
    not err
    and s0["settings"]["power"] == 0.42
    and s0["passes"] == 2
    and "power" in (redo.get("redone") or ""),
    texts[0][:160],
)

err, _, texts, _ = call(
    "set_step_settings", layer=0, step=0, settings={"power": 80}
)
check("rejects power=80", err, texts[0])

err, _, texts, _ = call(
    "set_step_settings", layer=0, step=0, settings={"laser_fire": True}
)
check("rejects unknown key", err, texts[0][:120])

if wp:
    err, moved, texts, _ = call(
        "set_workpiece", workpieces=[wp["uid"]], x=100, width=30
    )
    check(
        "set_workpiece: cut geometry x=100, width=30",
        not err
        and close(moved[0]["x"], 100)
        and close(moved[0]["width"], 30)
        and close(moved[0]["y"], 30),
        texts[0][:200],
    )

err, est, texts, _ = call("estimate_job")
check(
    "estimate_job",
    not err and est["estimated_seconds"],
    texts[0][:400]
    if err
    else f"{est['estimated_time']}, {est['total_distance_mm']} mm, "
    f"bbox {est.get('burn_bbox_mm')}, issues {est['sanity_issues']}",
)
if not err and est.get("burn_bbox_mm"):
    x0, y0 = est["burn_bbox_mm"][0], est["burn_bbox_mm"][1]
    check(
        "toolpath starts exactly where the geometry was placed",
        close(x0, 100) and close(y0, 30),
        est["burn_bbox_mm"],
    )

err, _, texts, images = call("preview_toolpath", width_px=800)
check("preview_toolpath", not err and images, texts[-1])
if images:
    with open(PREVIEW, "wb") as f:
        f.write(base64.b64decode(images[0]["data"]))
    print("      wrote", PREVIEW)

err, gcode, texts, _ = call("get_gcode", max_lines=15)
check(
    "get_gcode",
    not err and gcode["total_lines"] > 10,
    texts[0][:120] if err else f"{gcode['total_lines']} lines",
)
if not err:
    print("      " + gcode["head"].replace("\n", "\n      "))
    check(
        "estimate and get_gcode agree on line count",
        est.get("gcode_lines") == gcode["total_lines"],
        f"{est.get('gcode_lines')} vs {gcode['total_lines']}",
    )

err, undo, texts, _ = call("undo")
check("undo", not err, undo.get("undone") if not err else texts[0])

# --- Speed limits and choice settings --------------------------------
machine_info = docinfo["machine"]
max_travel = machine_info["max_travel_speed"]
max_cut = machine_info["max_cut_speed"]
check(
    "get_document reports the active WCS",
    "active" in (machine_info.get("work_coordinates") or {}),
    machine_info.get("work_coordinates"),
)
err, _, texts, _ = call(
    "set_step_settings",
    layer=0,
    step=0,
    settings={"travel_speed": max_travel + 1000},
)
check("rejects travel_speed above the machine max", err, texts[0][:120])

err, raster, texts, _ = call("add_step", layer=0, step_type="Raster")
check(
    "add_step keeps speeds within the machine's limits",
    not err
    and raster["settings"]["travel_speed"] <= max_travel
    and raster["settings"]["cut_speed"] <= max_cut,
    texts[0][:200]
    if err
    else f"{raster['settings']['cut_speed']}/"
    f"{raster['settings']['travel_speed']}, "
    f"adjusted {raster.get('speeds_limited_to_machine')}",
)
for value, expected in (
    ("Constant Power", "CONSTANT_POWER"),
    ("dither", "DITHER"),
    ("POWER_MODULATION", "POWER_MODULATION"),
):
    err, step, texts, _ = call(
        "set_step_settings",
        layer=0,
        step=raster["index"] if raster else 1,
        settings={"depth_mode": value},
    )
    check(
        f"depth_mode accepts {value!r}",
        not err and step["settings"]["depth_mode"] == expected,
        texts[0][:160],
    )
err, _, texts, _ = call(
    "set_step_settings",
    layer=0,
    step=raster["index"] if raster else 1,
    settings={"depth_mode": "laser beams"},
)
check("depth_mode rejects unknown choices", err, texts[0][:160])
err, _, texts, _ = call(
    "remove_step", layer=0, step=raster["index"] if raster else 1
)
check("remove_step", not err, texts[0][:120])

# Offset Fill can hang Rayforge on some shapes, so only try it on a
# layer without workpieces (nothing gets computed there).
err, scratch, texts, _ = call("add_layer", name="Offset Fill check")
err, fill, texts, _ = call(
    "add_step", layer=scratch["uid"], step_type="Offset Fill"
)
check(
    "Offset Fill steps carry a freeze warning",
    not err and any("freeze" in w for w in fill.get("warnings", [])),
    texts[0][:160] if err else fill.get("warnings"),
)
call("remove_layer", layer=scratch["uid"])

# --- Colour split, layers, per-layer time ----------------------------
COLOR_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="40mm"
     height="20mm" viewBox="0 0 40 20">
  <rect x="0" y="0" width="18" height="20" fill="none" stroke="#ff0000"/>
  <circle cx="30" cy="10" r="10" fill="none" stroke="#000000"/>
</svg>"""
err, split, texts, _ = call(
    "import_svg", svg=COLOR_SVG, split_by_color=True, x=150, y=100
)
new_layers = split.get("new_layers", []) if not err else []
check(
    "import_svg split_by_color makes a layer per colour",
    not err and len(new_layers) == 2 and len(split["imported"]) == 2,
    texts[0][:300] if err else [layer["name"] for layer in new_layers],
)
if not err and len(split["imported"]) == 2:
    xs = sorted((w["x"], w["y"]) for w in split["imported"])
    check(
        "split import placed as one design (layout kept)",
        close(xs[0][0], 150)
        and close(xs[0][1], 100)
        and close(xs[1][0], 170)
        and close(xs[1][1], 100),
        xs,
    )
    for layer in new_layers:
        for s in layer["steps"]:
            check(
                f"default step on {layer['name']} within limits",
                s["settings"].get("travel_speed", 0) <= max_travel,
                s["settings"].get("travel_speed"),
            )

if len(new_layers) == 2:
    red, black = new_layers
    err, upd, texts, _ = call(
        "update_layer", layer=red["uid"], name="Cut", index=0, visible=False
    )
    check(
        "update_layer renames, moves and hides in one call",
        not err
        and upd["name"] == "Cut"
        and upd["index"] == 0
        and upd["visible"] is False,
        texts[0][:200],
    )
    err, undo, texts, _ = call("undo")
    _e, d, _t, _i = call("get_document")
    names = [layer["name"] for layer in d["layers"]]
    check(
        "one undo reverts update_layer",
        not err and "Cut" not in names and red["name"] in names,
        names,
    )
    call("redo")

    err, est, texts, _ = call("estimate_job")
    by_layer = (
        {e["layer"]: e for e in est.get("by_layer", [])} if not err else {}
    )
    check(
        "estimate_job splits time by layer, hidden layer still included",
        "Cut" in by_layer
        and close(
            sum(e["seconds"] for e in by_layer.values()),
            est["estimated_seconds"],
            0.5,
        ),
        {k: v["seconds"] for k, v in by_layer.items()},
    )
    err, upd, texts, _ = call("update_layer", layer="Cut", enabled=False)
    err2, est2, _t, _i = call("estimate_job")
    check(
        "disabled layer leaves the job",
        not err
        and not err2
        and "Cut" not in {e["layer"] for e in est2.get("by_layer", [])}
        and est2["estimated_seconds"] < est["estimated_seconds"],
        f"{est['estimated_seconds']} -> {est2.get('estimated_seconds')}",
    )
    call("update_layer", layer="Cut", enabled=True)

    circle = [w for w in split["imported"] if w["width"] > 19][0]
    err, moved, texts, _ = call(
        "set_workpiece", workpieces=[circle["uid"]], layer="Cut"
    )
    check(
        "set_workpiece moves a workpiece to another layer",
        not err and moved[0]["layer"] == "Cut",
        texts[0][:160],
    )
    err, gone, texts, _ = call("remove_layer", layer=black["uid"])
    check("remove_layer", not err, texts[0][:160])

# --- Documents -------------------------------------------------------
err, _, texts, _ = call("new_document")
check("new_document refuses unsaved changes", err, texts[0][:160])
project = os.path.abspath(PREVIEW.rsplit(".", 1)[0] + "-smoke.ryp")
err, saved, texts, _ = call("save_project", path=project)
check("save_project", not err, texts[0][:160])
err, new, texts, _ = call("new_document")
_e, d, _t, _i = call("get_document")
check(
    "new_document after saving gives an empty document",
    not err and not any(layer["workpieces"] for layer in d["layers"]),
    texts[0][:160],
)
err, opened, texts, _ = call("open_project", path=project)
check(
    "open_project reopens the saved project",
    not err and any(layer["workpieces"] for layer in opened["layers"]),
    texts[0][:160],
)

err, ms, texts, _ = call("machine_status")
check("machine_status", not err, texts[0][:300])

err, info, texts, _ = call("machine_info")
check(
    "machine_info without a real machine",
    not err
    and info.get("controller_settings") is None
    and isinstance(info.get("heads"), list)
    and info.get("connected") is False,
    texts[0][:200] if err else info.get("controller_note"),
)

for name in ("machine_home", "machine_start_job", "machine_frame"):
    err, _, texts, _ = call(name)
    check(f"{name} refused without a real machine", err, texts[0][:120])

err, _, texts, _ = call("machine_jog", dx=0, dy=0)
check("machine_jog rejects zero move", err, texts[0][:120])

try:
    rpc("tools/call", {"name": "no_such_tool", "arguments": {}})
    check("unknown tool -> JSON-RPC error", False)
except RuntimeError as e:
    check("unknown tool -> JSON-RPC error", True, str(e))

print(f"\n{check.failed} failure(s)")
sys.exit(1 if check.failed else 0)
