"""
MCP tools that act on the open Rayforge document and machine.

Tool functions run on HTTP worker threads and use on_main() for every
access to Rayforge objects. Design edits go through Rayforge's undoable
commands. Machine tools that can move the head or fire the laser call
confirm(), which asks the operator in a dialog generated from the real
job, never from text the AI supplied.
"""

import base64
import dataclasses
import enum
import io
import json
import logging
import math
import mimetypes
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import cairo
from gi.repository import Adw, GLib
from raygeo.ops.axis import Axis
from raygeo.ops.types import CommandType

from rayforge.context import get_context
from rayforge.core.step_registry import step_registry
from rayforge.core.undo.command import Command
from rayforge.core.undo.list_cmd import ListItemCommand
from rayforge.machine.driver.dummy import NoDeviceDriver
from rayforge.machine.sanity.checker import SanityChecker
from rayforge.machine.sanity.result import CheckMode
from rayforge.pipeline.artifact import JobArtifact

from .settings import get_settings

logger = logging.getLogger(__name__)

MAX_SVG_BYTES = 5 * 1024 * 1024
IMPORT_DIR = Path(tempfile.gettempdir()) / "rayforge-mcp-imports"
# Treats Rayforge's "no device" placeholder driver as a machine, to try
# the approval dialogs without hardware.
DEMO = os.environ.get("RAYFORGE_MCP_DEMO") == "1"

win = None


def bind_window(window) -> None:
    global win
    win = window


# ---------------------------------------------------------------------
# Threading helpers
# ---------------------------------------------------------------------


class ToolError(Exception):
    """An error reported back to the MCP client as a failed tool call."""


def on_main(fn, timeout=30.0):
    """Run fn() on the GTK main thread and return its result."""
    done = threading.Event()
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised below
            box["error"] = e
        finally:
            done.set()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)
    if not done.wait(timeout):
        raise ToolError(f"Rayforge did not respond within {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def wait_for_callback(start, timeout):
    """
    Call start(callback) on the main thread and wait until callback is
    invoked (from any thread). Returns the callback's positional args.
    """
    done = threading.Event()
    box = {}

    def callback(*args):
        box["args"] = args
        done.set()

    on_main(lambda: start(callback))
    if not done.wait(timeout):
        raise ToolError(f"Timed out after {timeout:.0f}s")
    return box["args"]


def notify(message):
    """Show a toast in Rayforge (thread-safe)."""
    window = win
    if window is None:
        return

    def send():
        window.doc_editor.notification_requested.send(window, message=message)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(send)


def json_default(value):
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def to_json(value):
    """Round-trip through JSON so the result is plain data."""
    return json.loads(json.dumps(value, default=json_default))


def rnd(value, digits=3):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(value):
        return None
    return round(value, digits)


def fmt_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


# ---------------------------------------------------------------------
# Document model helpers (call on the main thread)
# ---------------------------------------------------------------------


def editor():
    return win.doc_editor


def doc():
    return win.doc_editor.doc


def machine():
    return get_context().machine


def find_layer(ref):
    layers = list(doc().layers)
    if ref is None or ref == "":
        return doc().active_layer
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        index = int(ref)
        if 0 <= index < len(layers):
            return layers[index]
    for layer in layers:
        if ref in (layer.uid, layer.name):
            return layer
    names = [f"{i}: {layer.name}" for i, layer in enumerate(layers)]
    raise ToolError(f"No layer {ref!r}. Layers are: {names}")


def layer_steps(layer):
    workflow = layer.workflow
    return list(workflow.steps) if workflow else []


def find_step(layer, ref):
    steps = layer_steps(layer)
    if not steps:
        raise ToolError(f"Layer {layer.name!r} has no steps. Use add_step.")
    if ref is None or ref == "":
        if len(steps) == 1:
            return steps[0]
        raise ToolError(
            f"Layer {layer.name!r} has {len(steps)} steps; pass 'step' "
            f"(index or name): {[s.name for s in steps]}"
        )
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        index = int(ref)
        if 0 <= index < len(steps):
            return steps[index]
    for step in steps:
        names = (step.uid, step.name, step.typelabel, type(step).__name__)
        if ref in names:
            return step
    raise ToolError(
        f"No step {ref!r} on layer {layer.name!r}. Steps are: "
        f"{[f'{i}: {s.name}' for i, s in enumerate(steps)]}"
    )


def find_workpieces(refs):
    if isinstance(refs, str):
        refs = [refs]
    if not refs:
        raise ToolError("Pass at least one workpiece uid or name.")
    all_items = list(doc().all_workpieces)
    found = []
    for ref in refs:
        matches = [wp for wp in all_items if ref in (wp.uid, wp.name)]
        if not matches:
            raise ToolError(f"No workpiece {ref!r}. Call get_document.")
        for wp in matches:
            if wp not in found:
                found.append(wp)
    return found


def multipass_dict(step):
    for t_dict in step.per_step_transformers_dicts:
        if t_dict.get("name") == "MultiPassTransformer":
            return t_dict
    return None


def step_settings(step):
    cls = type(step)
    values = {}
    for key in cls.recipe_keys():
        if hasattr(step, key):
            values[key] = cls.recipe_value(key, getattr(step, key))
    return values


def describe_step(step, index):
    mp = multipass_dict(step)
    passes = 1
    if mp is not None and mp.get("enabled", True):
        passes = int(mp.get("passes", 1))
    return {
        "index": index,
        "uid": step.uid,
        "name": step.name,
        "type": type(step).__name__,
        "visible": step.visible,
        "passes": passes,
        "settings": step_settings(step),
    }


def describe_workpiece(wp):
    transform = editor().transform
    pos = transform.get_position_group([wp]) or (None, None)
    size = transform.get_size_group([wp]) or (None, None)
    return {
        "uid": wp.uid,
        "name": wp.name,
        "x": rnd(pos[0]),
        "y": rnd(pos[1]),
        "width": rnd(size[0]),
        "height": rnd(size[1]),
        "angle": rnd(wp.angle),
    }


def describe_machine(m):
    if m is None:
        return None
    width, height = m.axis_extents
    return {
        "name": m.name,
        "driver": m.driver_name,
        "work_area_mm": [rnd(width), rnd(height)],
        "connected": m.is_connected(),
        "max_cut_speed": m.max_cut_speed,
        "max_travel_speed": m.max_travel_speed,
    }


def summarize_steps_for_humans():
    lines = []
    for layer in doc().layers:
        if not layer.visible:
            continue
        for step in layer_steps(layer):
            if not step.visible or not layer.all_workpieces:
                continue
            info = describe_step(step, 0)
            settings = info["settings"]
            parts = [f"{layer.name} / {step.name}"]
            if "power" in settings:
                parts.append(f"power {float(settings['power']) * 100:.0f}%")
            if "cut_speed" in settings:
                parts.append(f"{settings['cut_speed']} mm/min")
            if info["passes"] > 1:
                parts.append(f"{info['passes']} passes")
            lines.append("  - " + ", ".join(parts))
    return "\n".join(lines) or "  (no visible steps with workpieces)"


class RecipeValueCommand(Command):
    """Undoable change of one step setting via Step.set_recipe_value."""

    def __init__(self, step, key, new_value, old_value):
        super().__init__(name=f"MCP: set {key}")
        self.step = step
        self.key = key
        self.new_value = new_value
        self.old_value = old_value

    def execute(self):
        self.step.set_recipe_value(self.key, self.new_value)
        self.step.updated.send(self.step)

    def undo(self):
        self.step.set_recipe_value(self.key, self.old_value)
        self.step.updated.send(self.step)


def coerce_setting(var, key, value):
    """
    Validate and coerce a value using the step's own Var definition.
    The var must be a fresh instance (recipe_varset() builds new ones).
    """
    labels = getattr(var, "_label_to_value", None)
    if labels and isinstance(value, str) and value in labels:
        value = labels[value]
    try:
        var.value = value
    except (TypeError, ValueError) as e:
        raise ToolError(str(e)) from e
    coerced = var.value
    min_val = getattr(var, "min_val", None)
    max_val = getattr(var, "max_val", None)
    if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
        if (min_val is not None and coerced < min_val) or (
            max_val is not None and coerced > max_val
        ):
            hint = ""
            if key == "power":
                hint = " (power is a fraction: 0.8 = 80 %)"
            raise ToolError(
                f"{key}={value!r} is outside {min_val}..{max_val}{hint}"
            )
    choices = getattr(var, "choices", None)
    if choices:
        allowed = [c[1] if isinstance(c, tuple) else c for c in choices]
        names = [getattr(a, "name", a) for a in allowed]
        if coerced not in allowed and coerced not in names:
            raise ToolError(f"{key}={value!r} must be one of {names}")
    return coerced


def apply_placement(items, args):
    """Resize, rotate and move items per the optional tool arguments."""
    transform = editor().transform
    if args.get("width") is not None or args.get("height") is not None:
        transform.set_size(
            items,
            width=args.get("width"),
            height=args.get("height"),
            fixed_ratio=args.get("keep_aspect", True),
        )
    if args.get("angle") is not None:
        transform.set_angle(items, float(args["angle"]))
    if args.get("x") is not None or args.get("y") is not None:
        for item in items:
            cur_x, cur_y = transform.get_position_group([item])
            x = args.get("x")
            y = args.get("y")
            transform.set_position(
                [item],
                cur_x if x is None else float(x),
                cur_y if y is None else float(y),
            )


# ---------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------


def fetch_job(timeout=180.0):
    """Generate (or reuse) the job artifact and return plain data."""
    editor().wait_until_settled_sync(timeout=60.0)
    pipeline = editor().pipeline

    handle, error = wait_for_callback(
        lambda cb: pipeline.generate_job_artifact(when_done=cb), timeout
    )
    if error is not None:
        raise ToolError(f"Could not generate the job: {error}")
    if handle is None:
        raise ToolError("The document produces no toolpath.")

    def extract():
        with pipeline.artifact_store.checkout_handle(handle) as artifact:
            if not isinstance(artifact, JobArtifact):
                raise ToolError("Pipeline returned no job artifact.")
            return {
                "ops": artifact.ops.copy(),
                "gcode": artifact.machine_code,
                "time_estimate": getattr(handle, "time_estimate", None),
                "distance": getattr(handle, "distance", None),
            }

    job = on_main(extract)
    m = on_main(machine)
    if job["time_estimate"] is None and m is not None:
        job["time_estimate"] = job["ops"].estimate_time(
            default_feed_rate=m.max_cut_speed,
            default_rapid_rate=m.max_travel_speed,
            acceleration=m.acceleration,
        )
    return job


def job_summary(job):
    ops = job["ops"]
    summary = {
        "estimated_seconds": rnd(job["time_estimate"], 1),
        "estimated_time": fmt_duration(job["time_estimate"]),
        "total_distance_mm": rnd(job["distance"] or ops.distance(), 1),
    }
    for key, fn in (
        ("cut_distance_mm", "cut_distance"),
        ("bbox_mm", "rect"),
        ("burn_bbox_mm", "powered_rect"),
    ):
        try:
            value = getattr(ops, fn)()
        except Exception:  # noqa: BLE001 - optional statistics
            continue
        if isinstance(value, (tuple, list)):
            summary[key] = [rnd(v, 2) for v in value]
        else:
            summary[key] = rnd(value, 1)
    gcode = job.get("gcode")
    summary["gcode_lines"] = gcode.count("\n") + 1 if gcode else 0
    return summary


def sanity_issues(ops):
    def run():
        m = machine()
        if m is None:
            return []
        report = SanityChecker(m).check(ops, mode=CheckMode.FAST)
        return [
            {
                "severity": issue.severity.value,
                "category": issue.category.value,
                "message": issue.message,
            }
            for issue in report.issues
        ]

    return on_main(run)


def render_ops_png(ops, bed_size, width_px, show_travel):
    """Draw a linearized copy of the toolpath to a PNG (Y axis up)."""
    ops = ops.copy()
    ops.linearize_all()
    arrays = ops.to_numpy_arrays()
    types = arrays["types"]
    points = arrays["endpoints"]
    move_t = CommandType.MOVE_TO.value
    line_t = CommandType.LINE_TO.value

    xs = [0.0, float(bed_size[0])] if bed_size else []
    ys = [0.0, float(bed_size[1])] if bed_size else []
    try:
        x0, y0, x1, y1 = ops.rect()
        xs += [x0, x1]
        ys += [y0, y1]
    except Exception:  # noqa: BLE001 - empty ops
        pass
    if not xs:
        xs, ys = [0.0, 100.0], [0.0, 100.0]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)

    margin = 24
    width_px = int(min(max(width_px, 200), 2400))
    scale = (width_px - 2 * margin) / span_x
    height_px = int(span_y * scale + 2 * margin)
    if height_px > 2400:
        scale = (2400 - 2 * margin) / span_y
        height_px = 2400
        width_px = int(span_x * scale + 2 * margin)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width_px, height_px)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()

    def px(x, y):
        return margin + (x - min_x) * scale, margin + (max_y - y) * scale

    if bed_size:
        bx0, by0 = px(0, bed_size[1])
        ctx.rectangle(bx0, by0, bed_size[0] * scale, bed_size[1] * scale)
        ctx.set_source_rgb(0.95, 0.95, 0.93)
        ctx.fill_preserve()
        ctx.set_source_rgb(0.6, 0.6, 0.6)
        ctx.set_line_width(1)
        ctx.stroke()

    def draw(kind):
        prev = None
        for i in range(len(types)):
            t = types[i]
            if t != move_t and t != line_t:
                continue
            p = px(float(points[i][0]), float(points[i][1]))
            if t == kind and prev is not None:
                ctx.move_to(*prev)
                ctx.line_to(*p)
            prev = p

    if show_travel:
        ctx.set_source_rgba(0.2, 0.45, 0.9, 0.5)
        ctx.set_line_width(0.6)
        ctx.set_dash([3, 3])
        draw(move_t)
        ctx.stroke()
        ctx.set_dash([])

    ctx.set_source_rgb(0.85, 0.1, 0.1)
    ctx.set_line_width(1.2)
    draw(line_t)
    ctx.stroke()

    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue(), (width_px, height_px)


# ---------------------------------------------------------------------
# Operator confirmation
# ---------------------------------------------------------------------

_confirm_lock = threading.Lock()
_pending = {}

CONFIRM_FOOTER = (
    "\n\nRequested by an AI assistant over MCP. Only allow this if you "
    "are at the machine, the material and lid are ready, and you can "
    "stop it."
)


def confirm(heading, body):
    """Ask the operator in Rayforge; raise ToolError unless allowed."""
    if not _confirm_lock.acquire(blocking=False):
        raise ToolError("Another approval is already waiting in Rayforge.")
    timeout = get_settings().confirm_timeout
    try:
        done = threading.Event()
        box = {"answer": "deny"}

        def show():
            dialog = Adw.AlertDialog(
                heading=heading, body=body + CONFIRM_FOOTER
            )
            dialog.add_response("deny", "Deny")
            dialog.add_response("allow", "Allow")
            dialog.set_response_appearance(
                "allow", Adw.ResponseAppearance.DESTRUCTIVE
            )
            dialog.set_default_response("deny")
            dialog.set_close_response("deny")

            def on_response(_dialog, response):
                if not done.is_set():
                    box["answer"] = response
                    done.set()

            dialog.connect("response", on_response)
            win.present()
            dialog.present(win)
            box["dialog"] = dialog

        on_main(show)
        _pending.update(box=box, done=done)
        logger.info(f"Waiting for operator approval: {heading}")
        if not done.wait(timeout):
            on_main(lambda: box["dialog"].force_close())
            raise ToolError(
                f"No approval within {timeout}s. Nothing was sent to the "
                "machine."
            )
        if box["answer"] == "cancelled":
            raise ToolError(
                "The MCP server was stopped. Nothing was sent to the machine."
            )
        if box["answer"] != "allow":
            raise ToolError(
                "The operator denied this in Rayforge. Nothing was sent to "
                "the machine."
            )
        logger.info(f"Operator approved: {heading}")
    finally:
        _pending.clear()
        _confirm_lock.release()


def cancel_pending_confirmation():
    """
    Deny and close a waiting approval dialog. Called on the main thread
    when the server stops, so a late click cannot start an action.
    """
    box = _pending.get("box")
    done = _pending.get("done")
    if box is None or done is None or done.is_set():
        return
    box["answer"] = "cancelled"
    done.set()
    dialog = box.get("dialog")
    if dialog is not None:
        dialog.force_close()


def require_machine(connected=True, idle=True):
    m = machine()
    if m is None:
        raise ToolError("No machine is configured in Rayforge.")
    if connected and isinstance(m.driver, NoDeviceDriver) and not DEMO:
        raise ToolError(
            f"Machine {m.name!r} has no device driver configured. Set one "
            "up in Rayforge's machine settings."
        )
    if connected and not m.is_connected():
        raise ToolError(
            f"Machine {m.name!r} is not connected. Connect it in Rayforge."
        )
    if idle and win.machine_cmd.is_job_running:
        raise ToolError("A job is running. Pause or stop it first.")
    return m


def window_action(name):
    action = win.action_manager.get_action(name)
    if action is None:
        raise ToolError(f"Rayforge has no action {name!r}.")
    if not action.get_enabled():
        raise ToolError(
            f"Rayforge has {name!r} disabled right now (machine busy, "
            "not ready, or nothing to send)."
        )
    return action


def position_text(m):
    pos = m.device_state.work_pos or (None, None, None)
    if pos[0] is None:
        return "unknown"
    return ", ".join(
        f"{a}={rnd(v, 2)}" for a, v in zip("XYZ", pos) if v is not None
    )


# ---------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------

TOOLS = {}


class Image:
    """A tool result that carries a PNG plus a text caption."""

    def __init__(self, png_bytes, caption):
        self.png_bytes = png_bytes
        self.caption = caption


def tool(
    name,
    description,
    properties=None,
    required=(),
    *,
    read_only=False,
    destructive=False,
    machine_control=False,
    bypass_lock=False,
):
    def register(fn):
        TOOLS[name] = {
            "fn": fn,
            "bypass_lock": bypass_lock,
            "machine_control": machine_control,
            "spec": {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties or {},
                    "required": list(required),
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": read_only,
                    "destructiveHint": destructive,
                    "openWorldHint": machine_control,
                },
            },
        }
        return fn

    return register


def list_tools():
    machine_tools = get_settings().machine_tools
    return [
        entry["spec"]
        for entry in TOOLS.values()
        if machine_tools or not entry["machine_control"]
    ]


_tool_lock = threading.Lock()


def call_tool(name, args):
    """Run a tool; return an MCP CallToolResult, or None if unknown."""
    entry = TOOLS.get(name)
    if entry is None:
        return None
    logger.info(f"MCP tool call: {name} {json.dumps(args)[:300]}")
    if entry["machine_control"] and not get_settings().machine_tools:
        return error_result(
            "Machine control is turned off in Rayforge "
            "(Settings > MCP Bridge)."
        )
    if win is None:
        return error_result("Rayforge's main window is not ready yet.")
    try:
        if entry["bypass_lock"]:
            value = entry["fn"](args)
        else:
            with _tool_lock:
                value = entry["fn"](args)
    except ToolError as e:
        return error_result(str(e))
    except Exception as e:  # noqa: BLE001 - report any failure to client
        logger.exception(f"MCP tool {name} failed")
        return error_result(f"{type(e).__name__}: {e}")
    if isinstance(value, Image):
        data = base64.b64encode(value.png_bytes).decode("ascii")
        return {
            "content": [
                {"type": "image", "data": data, "mimeType": "image/png"},
                {"type": "text", "text": value.caption},
            ]
        }
    text = json.dumps(to_json(value), indent=1)
    return {"content": [{"type": "text", "text": text}]}


def error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


LAYER_ARG = {
    "type": ["integer", "string"],
    "description": "Layer index (0-based), name or uid. Default: active.",
}
STEP_ARG = {
    "type": ["integer", "string"],
    "description": "Step index (0-based), name, type or uid. Optional "
    "when the layer has exactly one step.",
}
WORKPIECES_ARG = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Workpiece uids or names.",
}
NUMBER = {"type": "number"}


# --- Design tools -----------------------------------------------------


@tool(
    "get_document",
    "Return the machine, layers, steps (with power/speed/passes) and "
    "workpieces (position and size in mm) of the open Rayforge document.",
    read_only=True,
)
def t_get_document(args):
    def run():
        d = doc()
        active = d.active_layer
        layers = []
        for i, layer in enumerate(d.layers):
            steps = layer_steps(layer)
            layers.append(
                {
                    "index": i,
                    "uid": layer.uid,
                    "name": layer.name,
                    "active": layer is active,
                    "visible": layer.visible,
                    "steps": [
                        describe_step(s, j) for j, s in enumerate(steps)
                    ],
                    "workpieces": [
                        describe_workpiece(wp) for wp in layer.all_workpieces
                    ],
                }
            )
        return {
            "file": editor().file_path,
            "saved": editor().is_saved,
            "machine": describe_machine(machine()),
            "layers": layers,
        }

    return on_main(run)


@tool(
    "list_step_types",
    "List the step (operation) types that can be added to a layer, "
    "e.g. Contour (cut/score lines) or Raster (engrave fills/images).",
    read_only=True,
)
def t_list_step_types(args):
    def run():
        m = machine()
        caps = m.get_capabilities() if m else None
        out = []
        for name, cls in step_registry.all_steps().items():
            if cls.HIDDEN:
                continue
            out.append(
                {
                    "type": name,
                    "label": getattr(cls, "TYPELABEL", name),
                    "available_on_machine": caps is None
                    or cls.REQUIRED_MACHINE_CAPS.issubset(caps),
                }
            )
        return out

    return on_main(run)


@tool(
    "describe_step_settings",
    "Describe every setting of a step: key, label, description, current "
    "value, default, allowed range or choices. Use before "
    "set_step_settings.",
    {"layer": LAYER_ARG, "step": STEP_ARG},
    read_only=True,
)
def t_describe_step_settings(args):
    def run():
        layer = find_layer(args.get("layer"))
        step = find_step(layer, args.get("step"))
        current = step_settings(step)
        settings = []
        for var in type(step).recipe_varset():
            entry = {
                "key": var.key,
                "label": str(var.label),
                "description": str(var.description or ""),
                "value": current.get(var.key),
                "default": var.default,
            }
            for attr in ("min_val", "max_val"):
                if getattr(var, attr, None) is not None:
                    entry[attr.replace("_val", "")] = getattr(var, attr)
            labels = getattr(var, "_value_to_label", None)
            if labels:
                entry["choices"] = {
                    str(getattr(k, "name", k)): str(v)
                    for k, v in labels.items()
                }
            elif getattr(var, "choices", None):
                entry["choices"] = list(var.choices)
            settings.append(entry)
        if multipass_dict(step) is not None:
            settings.append(
                {
                    "key": "passes",
                    "label": "Passes",
                    "description": "Number of times the step is repeated.",
                    "value": describe_step(step, 0)["passes"],
                    "default": 1,
                    "min": 1,
                }
            )
        return {"layer": layer.name, "step": step.name, "settings": settings}

    return on_main(run)


@tool(
    "set_step_settings",
    'Change settings of a step, e.g. {"power": 0.6, "cut_speed": 800, '
    '"passes": 2}. power is a fraction 0.0-1.0; speeds are mm/min. Each '
    "change is undoable.",
    {
        "layer": LAYER_ARG,
        "step": STEP_ARG,
        "settings": {
            "type": "object",
            "description": "Setting key -> new value (see "
            "describe_step_settings).",
        },
    },
    required=("settings",),
)
def t_set_step_settings(args):
    settings = args.get("settings") or {}
    if not isinstance(settings, dict) or not settings:
        raise ToolError("'settings' must be a non-empty object.")

    def run():
        layer = find_layer(args.get("layer"))
        step = find_step(layer, args.get("step"))
        cls = type(step)
        varset = {var.key: var for var in cls.recipe_varset()}
        planned = []
        for key, value in settings.items():
            if key == "passes":
                t_dict = multipass_dict(step)
                if t_dict is None:
                    raise ToolError(f"{step.name!r} has no passes setting.")
                passes = int(value)
                if passes < 1:
                    raise ToolError("passes must be >= 1")
                planned.append(("passes", t_dict, passes))
                continue
            if key not in varset:
                raise ToolError(
                    f"Unknown setting {key!r} for {cls.__name__}. Valid: "
                    f"{sorted(varset) + ['passes']}"
                )
            new_value = coerce_setting(varset[key], key, value)
            old_value = cls.recipe_value(key, getattr(step, key, None))
            planned.append((key, step, (new_value, old_value)))

        history = editor().history_manager
        on_transformer_change = step.per_step_transformer_changed.send
        for key, target, payload in planned:
            if key != "passes":
                new_value, old_value = payload
                history.execute(
                    RecipeValueCommand(target, key, new_value, old_value)
                )
                continue
            if payload > 1 and target.get("enabled") is False:
                editor().step.set_step_param(
                    target_dict=target,
                    key="enabled",
                    new_value=True,
                    name="MCP: enable passes",
                    on_change_callback=on_transformer_change,
                )
            editor().step.set_step_param(
                target_dict=target,
                key="passes",
                new_value=payload,
                name="MCP: change number of passes",
                on_change_callback=on_transformer_change,
            )
        return describe_step(step, layer_steps(layer).index(step))

    result = on_main(run)
    editor().wait_until_settled_sync(timeout=30.0)
    return result


@tool(
    "add_layer",
    "Add a new layer (it becomes the active layer, so imports land on it).",
    {"name": {"type": "string"}},
)
def t_add_layer(args):
    def run():
        layer_cmd = editor().layer
        before = {layer.uid for layer in doc().layers}
        layer_cmd.add_layer_and_set_active()
        new = [layer for layer in doc().layers if layer.uid not in before]
        if not new:
            raise ToolError("Rayforge did not add a layer.")
        layer = new[0]
        if args.get("name"):
            layer_cmd.rename_layer(layer, str(args["name"]))
        steps = layer_steps(layer)
        return {
            "index": list(doc().layers).index(layer),
            "uid": layer.uid,
            "name": layer.name,
            "steps": [describe_step(s, j) for j, s in enumerate(steps)],
        }

    return on_main(run)


@tool(
    "set_active_layer",
    "Make a layer the active one. New imports go to the active layer.",
    {"layer": LAYER_ARG},
    required=("layer",),
)
def t_set_active_layer(args):
    def run():
        layer = find_layer(args["layer"])
        editor().layer.set_active_layer(layer)
        return {"active_layer": layer.name}

    return on_main(run)


@tool(
    "add_step",
    "Add a step (operation) to a layer's workflow, e.g. step_type "
    '"ContourStep" or "Contour". See list_step_types.',
    {"layer": LAYER_ARG, "step_type": {"type": "string"}},
    required=("step_type",),
)
def t_add_step(args):
    def run():
        layer = find_layer(args.get("layer"))
        if layer.workflow is None:
            raise ToolError(f"Layer {layer.name!r} has no workflow.")
        wanted = str(args["step_type"])
        cls = step_registry.get(wanted) or step_registry.get_by_typelabel(
            wanted
        )
        if cls is None:
            for name, candidate in step_registry.all_steps().items():
                aliases = (
                    name.lower(),
                    name.lower().removesuffix("step"),
                    str(getattr(candidate, "TYPELABEL", "")).lower(),
                )
                if wanted.lower() in aliases:
                    cls = candidate
                    break
        if cls is None or cls.HIDDEN:
            raise ToolError(
                f"Unknown step type {wanted!r}. See list_step_types."
            )
        step = cls.create(get_context())
        editor().step.apply_best_recipe_to_step(step)
        editor().history_manager.execute(
            ListItemCommand(
                owner_obj=layer.workflow,
                item=step,
                undo_command="remove_step",
                redo_command="add_step",
                name=f"MCP: add step {step.name}",
            )
        )
        return describe_step(step, layer_steps(layer).index(step))

    return on_main(run)


@tool(
    "remove_step",
    "Remove a step from a layer's workflow (undoable).",
    {"layer": LAYER_ARG, "step": STEP_ARG},
    required=("step",),
    destructive=True,
)
def t_remove_step(args):
    def run():
        layer = find_layer(args.get("layer"))
        step = find_step(layer, args.get("step"))
        editor().history_manager.execute(
            ListItemCommand(
                owner_obj=layer.workflow,
                item=step,
                undo_command="add_step",
                redo_command="remove_step",
                name=f"MCP: remove step {step.name}",
            )
        )
        return {"removed": step.name, "layer": layer.name}

    return on_main(run)


def import_path(path, mime_type, args, timeout=60.0):
    """Import a file into the active layer and return the new items."""
    if args.get("layer") is not None:
        on_main(
            lambda: editor().layer.set_active_layer(find_layer(args["layer"]))
        )

    before = on_main(lambda: {wp.uid for wp in doc().all_workpieces})
    on_main(
        lambda: editor().file.load_file_from_path(Path(path), mime_type, None)
    )

    deadline = time.monotonic() + timeout
    new_uids = set()
    while time.monotonic() < deadline:
        time.sleep(0.2)
        new_uids = on_main(
            lambda: {wp.uid for wp in doc().all_workpieces} - before
        )
        if new_uids:
            break
    if not new_uids:
        raise ToolError(
            "The import produced no objects. Check Rayforge's notification "
            "or log for the reason."
        )
    editor().wait_until_settled_sync(timeout=30.0)

    def place():
        items = [wp for wp in doc().all_workpieces if wp.uid in new_uids]
        apply_placement(items, args)
        return [describe_workpiece(wp) for wp in items]

    items = on_main(place)
    editor().wait_until_settled_sync(timeout=30.0)
    return {"imported": items}


PLACEMENT_ARGS = {
    "x": {"type": "number", "description": "Target X in mm (optional)."},
    "y": {"type": "number", "description": "Target Y in mm (optional)."},
    "width": {"type": "number", "description": "Resize to width mm."},
    "height": {"type": "number", "description": "Resize to height mm."},
    "keep_aspect": {"type": "boolean", "default": True},
    "layer": LAYER_ARG,
}


@tool(
    "import_svg",
    "Import SVG markup as new workpiece(s) on the active (or given) "
    'layer. Give the <svg> real-world size, e.g. width="50mm" '
    'height="30mm" with a matching viewBox. Optionally place/resize.',
    {
        "svg": {"type": "string", "description": "Complete SVG document."},
        "name": {"type": "string", "description": "Name for the import."},
        **PLACEMENT_ARGS,
    },
    required=("svg",),
)
def t_import_svg(args):
    svg = args["svg"]
    if "<svg" not in svg:
        raise ToolError("'svg' must contain an <svg> element.")
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise ToolError("SVG is larger than 5 MB.")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.get("name") or "mcp-import")
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = IMPORT_DIR / f"{name}-{int(time.time() * 1000)}.svg"
    path.write_text(svg, encoding="utf-8")
    return import_path(path, "image/svg+xml", args)


@tool(
    "import_file",
    "Import a file from this computer (SVG, DXF, PDF, PNG, JPG, ...) as "
    "new workpiece(s). Optionally place/resize.",
    {
        "path": {"type": "string", "description": "Absolute file path."},
        **PLACEMENT_ARGS,
    },
    required=("path",),
)
def t_import_file(args):
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        raise ToolError(f"File not found: {path}")
    mime_type, _ = mimetypes.guess_type(str(path))
    return import_path(path, mime_type, args)


@tool(
    "set_workpiece",
    "Move, resize or rotate workpieces. Position is the X/Y shown in "
    "Rayforge (mm). Only the given fields change.",
    {
        "workpieces": WORKPIECES_ARG,
        "x": NUMBER,
        "y": NUMBER,
        "width": NUMBER,
        "height": NUMBER,
        "angle": {"type": "number", "description": "Degrees."},
        "keep_aspect": {"type": "boolean", "default": True},
    },
    required=("workpieces",),
)
def t_set_workpiece(args):
    def run():
        items = find_workpieces(args["workpieces"])
        apply_placement(items, args)
        return [describe_workpiece(wp) for wp in items]

    result = on_main(run)
    editor().wait_until_settled_sync(timeout=30.0)
    return result


@tool(
    "remove_workpieces",
    "Delete workpieces from the document (undoable).",
    {"workpieces": WORKPIECES_ARG},
    required=("workpieces",),
    destructive=True,
)
def t_remove_workpieces(args):
    def run():
        items = find_workpieces(args["workpieces"])
        editor().edit.remove_items(items, "MCP: remove item(s)")
        return {"removed": [wp.name for wp in items]}

    return on_main(run)


@tool("undo", "Undo the last change in Rayforge.")
def t_undo(args):
    def run():
        history = editor().history_manager
        if not history.can_undo():
            raise ToolError("Nothing to undo.")
        history.undo()
        return {"can_undo": history.can_undo(), "can_redo": history.can_redo()}

    return on_main(run)


@tool("redo", "Redo the last undone change in Rayforge.")
def t_redo(args):
    def run():
        history = editor().history_manager
        if not history.can_redo():
            raise ToolError("Nothing to redo.")
        history.redo()
        return {"can_undo": history.can_undo(), "can_redo": history.can_redo()}

    return on_main(run)


@tool(
    "estimate_job",
    "Generate the toolpath for the whole document and return the "
    "estimated run time, distances, bounding boxes and Rayforge's "
    "sanity-check warnings (out of bounds, no-go zones).",
    read_only=True,
)
def t_estimate_job(args):
    job = fetch_job()
    summary = job_summary(job)
    summary["sanity_issues"] = sanity_issues(job["ops"])
    summary["steps"] = on_main(summarize_steps_for_humans).splitlines()
    return summary


@tool(
    "preview_toolpath",
    "Render the generated toolpath as a PNG: red = burning moves, dashed "
    "blue = travel moves, grey = machine work area.",
    {
        "width_px": {"type": "integer", "default": 900},
        "show_travel": {"type": "boolean", "default": True},
    },
    read_only=True,
)
def t_preview_toolpath(args):
    job = fetch_job()
    bed = on_main(lambda: machine().axis_extents if machine() else None)
    png, (w, h) = render_ops_png(
        job["ops"],
        bed,
        int(args.get("width_px", 900)),
        bool(args.get("show_travel", True)),
    )
    summary = job_summary(job)
    caption = (
        f"Toolpath preview {w}x{h}px, estimated {summary['estimated_time']}, "
        f"{summary['total_distance_mm']} mm total."
    )
    return Image(png, caption)


@tool(
    "get_gcode",
    "Return the start of the generated machine code, and optionally save "
    "all of it to a file.",
    {
        "max_lines": {"type": "integer", "default": 120},
        "save_to": {"type": "string", "description": "Optional file path."},
    },
)
def t_get_gcode(args):
    job = fetch_job()
    gcode = job.get("gcode")
    if not gcode:
        raise ToolError(
            "This machine's driver does not produce text machine code."
        )
    lines = gcode.splitlines()
    result = {"total_lines": len(lines)}
    if args.get("save_to"):
        target = Path(args["save_to"]).expanduser()
        target.write_text(gcode, encoding="utf-8")
        result["saved_to"] = str(target)
    max_lines = max(0, int(args.get("max_lines", 120)))
    result["head"] = "\n".join(lines[:max_lines])
    return result


@tool(
    "save_project",
    "Save the document as a Rayforge project (.ryp). Without a path it "
    "saves to the current file.",
    {"path": {"type": "string"}},
)
def t_save_project(args):
    def run():
        target = args.get("path") or editor().file_path
        if not target:
            raise ToolError("The document has no file yet; pass 'path'.")
        target = Path(target).expanduser()
        if target.suffix.lower() != ".ryp":
            target = target.with_suffix(".ryp")
        if not editor().file.save_project_to_path(target):
            raise ToolError("Saving failed; see Rayforge's notification.")
        return {"saved_to": str(target)}

    return on_main(run)


# --- Machine tools ------------------------------------------------------


@tool(
    "machine_status",
    "Connection, controller state (IDLE/RUN/HOLD/ALARM...), position and "
    "progress of the running job.",
    read_only=True,
    bypass_lock=True,
)
def t_machine_status(args):
    def run():
        m = machine()
        if m is None:
            return {"machine": None}
        state = m.device_state
        hold = win.action_manager.get_action("machine-hold")
        return {
            "machine": describe_machine(m),
            "connection": m.connection_status,
            "status": state.status,
            "error": state.error,
            "work_position": state.work_pos,
            "machine_position": state.machine_pos,
            "paused": bool(hold and hold.get_state().get_boolean()),
            "job_running": win.machine_cmd.is_job_running,
            "job": m.current_job,
        }

    return on_main(run)


@tool(
    "machine_stop",
    "Stop/cancel the running job immediately. Never needs approval.",
    destructive=True,
    bypass_lock=True,
)
def t_machine_stop(args):
    def run():
        require_machine(connected=False, idle=False)
        win.action_manager.get_action("machine-cancel").activate(None)
        return {"stopped": True}

    result = on_main(run)
    notify("AI assistant stopped the job")
    return result


@tool(
    "machine_pause",
    "Pause (feed hold) the running job. Never needs approval.",
    bypass_lock=True,
)
def t_machine_pause(args):
    def run():
        require_machine(idle=False)
        action = window_action("machine-hold")
        action.change_state(GLib.Variant.new_boolean(True))
        return {"paused": True}

    return on_main(run)


@tool(
    "machine_resume",
    "Resume a paused job. Needs the operator's approval in Rayforge.",
    destructive=True,
    machine_control=True,
)
def t_machine_resume(args):
    def check():
        m = require_machine(idle=False)
        window_action("machine-hold")
        return m.name

    name = on_main(check)
    confirm("Resume the paused job?", f"Machine: {name}")

    def run():
        action = window_action("machine-hold")
        action.change_state(GLib.Variant.new_boolean(False))
        return {"resumed": True}

    return on_main(run)


@tool(
    "machine_home",
    "Home the machine. Needs the operator's approval in Rayforge.",
    destructive=True,
    machine_control=True,
)
def t_machine_home(args):
    def check():
        m = require_machine()
        if not m.can_home():
            raise ToolError("This machine cannot home.")
        window_action("machine-home")
        return m.name

    name = on_main(check)
    confirm("Home the machine?", f"Machine: {name}\nAll axes will move.")
    on_main(lambda: window_action("machine-home").activate(None))
    return {"homing": True}


@tool(
    "machine_jog",
    "Move the head by dx/dy mm (machine axis directions) with the laser "
    "off. Needs the operator's approval in Rayforge.",
    {
        "dx": {"type": "number", "default": 0},
        "dy": {"type": "number", "default": 0},
        "speed": {"type": "integer", "description": "mm/min (optional)"},
    },
    destructive=True,
    machine_control=True,
)
def t_machine_jog(args):
    dx = float(args.get("dx", 0) or 0)
    dy = float(args.get("dy", 0) or 0)
    if not (math.isfinite(dx) and math.isfinite(dy)) or (dx == 0 and dy == 0):
        raise ToolError("Give a non-zero, finite dx and/or dy.")

    def check():
        m = require_machine()
        deltas = {}
        for axis, delta in ((Axis.X, dx), (Axis.Y, dy)):
            if delta == 0:
                continue
            if not m.can_jog(axis):
                raise ToolError(f"This machine cannot jog {axis.name}.")
            if m.would_jog_exceed_limits(axis, delta):
                raise ToolError(
                    f"Jogging {axis.name} by {delta} mm exceeds the work area."
                )
            deltas[axis] = delta
        speed = int(args.get("speed") or m.jog_speed)
        speed = max(1, min(speed, int(m.max_travel_speed)))
        return m, deltas, speed, position_text(m)

    m, deltas, speed, where = on_main(check)
    moves = ", ".join(f"{a.name} {d:+.2f} mm" for a, d in deltas.items())
    confirm(
        "Jog the laser head?",
        f"Machine: {m.name}\nMove: {moves} at {speed} mm/min (laser off)\n"
        f"Current position: {where}",
    )
    on_main(lambda: win.machine_cmd.jog(m, deltas, speed))
    return {"jogging": moves, "speed": speed}


@tool(
    "machine_frame",
    "Trace the job's outline on the machine so the operator can check "
    "placement (may use low laser power if the machine is set up for "
    "it). Needs the operator's approval in Rayforge.",
    destructive=True,
    machine_control=True,
)
def t_machine_frame(args):
    on_main(require_machine)
    job = fetch_job()
    summary = job_summary(job)

    def details():
        m = machine()
        head = m.get_default_laser_head()
        power = getattr(head, "frame_power_percent", 0) or 0
        return m.name, power

    name, frame_power = on_main(details)
    bbox = summary.get("burn_bbox_mm") or summary.get("bbox_mm")
    confirm(
        "Frame the job outline?",
        f"Machine: {name}\nOutline (mm): {bbox}\n"
        f"Frame laser power: {float(frame_power) * 100:.0f}%",
    )

    def run():
        require_machine()
        win._start_frame_job(include_overscan=False)
        return {"framing": True, "outline_mm": bbox}

    return on_main(run)


@tool(
    "machine_start_job",
    "Generate the toolpath and send the job to the machine (the laser "
    "will fire). Needs the operator's approval in Rayforge, which shows "
    "the job's estimated time, size and per-step power/speed.",
    destructive=True,
    machine_control=True,
)
def t_machine_start_job(args):
    on_main(lambda: (require_machine(), window_action("machine-send")))
    job = fetch_job()
    summary = job_summary(job)
    issues = sanity_issues(job["ops"])
    steps_text = on_main(summarize_steps_for_humans)
    name = on_main(lambda: machine().name)
    warn = ""
    if issues:
        warn = "\n\nSanity check:\n" + "\n".join(
            f"  - {i['severity'].upper()}: {i['message']}" for i in issues
        )
    confirm(
        "Start the laser job?",
        f"Machine: {name}\n"
        f"Estimated time: {summary['estimated_time']}\n"
        f"Burn area (mm): {summary.get('burn_bbox_mm')}\n"
        f"Steps:\n{steps_text}{warn}",
    )
    on_main(lambda: window_action("machine-send").activate(None))
    notify("AI assistant started the job (approved by operator)")
    return {
        "submitted": True,
        "estimated_time": summary["estimated_time"],
        "note": "Rayforge may still show its own sanity-check dialog. "
        "Use machine_status to follow progress.",
    }


@tool(
    "machine_clear_alarm",
    "Clear/unlock a controller alarm. Needs the operator's approval.",
    destructive=True,
    machine_control=True,
)
def t_machine_clear_alarm(args):
    def check():
        m = require_machine(idle=False)
        window_action("machine-clear-alarm")
        return m.name, m.device_state.status, m.device_state.error

    name, status, error = on_main(check)
    confirm(
        "Clear the machine alarm?",
        f"Machine: {name}\nState: {getattr(status, 'name', status)}\n"
        f"Error: {error or 'none'}\nClearing an alarm can unlock motion "
        "without homing.",
    )
    on_main(lambda: window_action("machine-clear-alarm").activate(None))
    return {"cleared": True}
