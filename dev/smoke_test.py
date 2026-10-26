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
