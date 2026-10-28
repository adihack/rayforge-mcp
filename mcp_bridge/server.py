"""
MCP over Streamable HTTP (JSON responses, no server-initiated stream),
and the controller that starts and stops the server.
"""

import errno
import gc
import hmac
import json
import logging
import sys
import threading
from gettext import gettext as _
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from blinker import Signal
from gi.repository import GLib

from rayforge.context import get_context

from . import ADDON_NAME, VERSION, tools
from .settings import MCP_PATH, get_settings, save_settings

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 20 * 1024 * 1024
SERVER_INFO = {"name": "rayforge-mcp-bridge", "version": VERSION}
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Settings that only take effect after the listening socket is reopened.
RESTART_KEYS = {"port", "allow_lan"}
# Process-wide slot (on the Rayforge context, which outlives addon
# reloads) holding the controller of the currently loaded copy.
CONTROLLER_SLOT = "_mcp_bridge_controller"

_THIS_MODULE = sys.modules[__name__]


def is_current_copy() -> bool:
    """
    False once this copy of the addon was uninstalled or replaced.

    Rayforge uninstalls and upgrades addons by dropping their modules
    without calling on_unload, so a server can outlive its addon.
    """
    return (
        sys.modules.get(__name__) is _THIS_MODULE
        and ADDON_NAME in get_context().addon_mgr.loaded_addons
    )


INSTRUCTIONS = """\
You are connected to Rayforge, a laser cutter/engraver app, through a \
bridge running inside the app. Changes appear live in the user's window \
and can be undone (tool "undo").

Units: millimetres, and mm/min for speeds. A workpiece's x/y/width/\
height describe the geometry the laser follows, in machine coordinates \
of its origin corner; "frame" is the slightly larger box Rayforge shows \
in its X/Y fields. "power" is a fraction 0.0-1.0 (0.8 = 80 %).

Model: the document has layers; each layer has workpieces (imported \
shapes/images) and a workflow of steps (Contour, Raster, ...) that \
turn its workpieces into toolpaths. Call get_document first.

Typical flow: get_document -> import_svg -> set_step_settings -> \
estimate_job / preview_toolpath -> machine_start_job.

Layers run in order. Hiding a layer (visible=false) only affects the \
canvas; hidden layers are still burned. To frame or run only some \
layers, disable the others with update_layer(enabled=false) and enable \
them again afterwards. To cut or engrave by colour, use \
import_svg(split_by_color=true) and set each new layer's steps.

Avoid Offset Fill for text and detailed shapes: on some shapes it \
freezes Rayforge (the app stops responding and has to be closed). Use \
Engrave (raster) for fills. Speeds above the machine's maximum are refused.

Before the first machine action on a machine, call machine_info and \
tell the user about any "checks" it reports (e.g. GRBL laser mode off).

Controller settings ($N on GRBL): change them with \
set_controller_setting, which reads them back. Rayforge's own machine \
profile (max speeds, acceleration, frame power/speed) is changed with \
set_machine_settings. machine_send_command sends one console line; \
read-only queries ($$, $#, $G, $I) need no approval, anything else does.

Machine actions (home, jog, frame, start, resume, clear alarm) wait for \
the operator to click Allow in Rayforge. If one is denied or times out, \
do not retry it in a loop: tell the user. machine_stop never needs \
approval; use it if anything looks wrong.

Some controllers stop reporting while they home and keep showing IDLE. \
machine_status's awaiting_ok lists commands the controller has not \
answered yet (e.g. "$H"); motion is refused until it is empty.
"""


# ---------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def rpc_error(msg_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def handle_message(msg):
    """Handle one JSON-RPC message; return a response dict or None."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Invalid Request")
    method = msg.get("method")
    if method is None:
        return None  # a response to a server request; we never send any
    msg_id = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}
    try:
        result = dispatch(method, params)
    except RpcError as e:
        if is_notification:
            return None
        return rpc_error(msg_id, e.code, e.message)
    if is_notification or result is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def dispatch(method, params):
    if method == "initialize":
        requested = params.get("protocolVersion")
        if requested in PROTOCOL_VERSIONS:
            version = requested
        else:
            version = PROTOCOL_VERSIONS[0]
        client = params.get("clientInfo", {}).get("name", "unknown")
        logger.info(f"MCP client connected: {client} ({version})")
        tools.notify(
            _("AI assistant connected: {client}").format(client=client)
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools.list_tools()}
    if method == "tools/call":
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(-32602, "arguments must be an object")
        result = tools.call_tool(params.get("name"), args)
        if result is None:
            raise RpcError(-32602, f"Unknown tool: {params.get('name')}")
        return result
    if method.startswith("notifications/"):
        return None
    raise RpcError(-32601, f"Method not found: {method}")


# ---------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"RayforgeMCP/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.debug("HTTP " + fmt % args)

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status == 405:
            self.send_header("Allow", "POST")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _authorized(self):
        origin = self.headers.get("Origin")
        if origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS:
            self._send_json(403, rpc_error(None, -32000, "Origin not allowed"))
            return False
        token = get_settings().token
        if token:
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                auth.encode(), f"Bearer {token}".encode()
            ):
                self._send_json(401, rpc_error(None, -32001, "Unauthorized"))
                return False
        return True

    def do_GET(self):
        if self.path.split("?")[0] == MCP_PATH:
            self._send(405)
            return
        if self.path == "/":
            text = (
                f"Rayforge MCP bridge {VERSION}: POST JSON-RPC to "
                f"{MCP_PATH}.\n"
            )
            self._send(200, text.encode(), "text/plain; charset=utf-8")
            return
        self._send(404)

    def do_DELETE(self):
        self._send(405)

    def do_POST(self):
        if self.path.split("?")[0] != MCP_PATH:
            self._send(404)
            return
        if not is_current_copy():
            message = "This copy of the MCP Bridge addon was removed."
            self._send_json(503, rpc_error(None, -32002, message))
            self.server.retire()
            return
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            status = 413 if length > 0 else 400
            self._send_json(status, rpc_error(None, -32600, "Bad body size"))
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, rpc_error(None, -32700, "Parse error"))
            return

        if isinstance(payload, list):
            body = [r for r in map(handle_message, payload) if r] or None
        else:
            body = handle_message(payload)
        if body is None:
            self._send(202)
        else:
            self._send_json(200, body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, controller):
        super().__init__(address, handler)
        self.controller = controller
        self._retiring = False

    def retire(self):
        """Stop this server (once) because its addon copy is gone."""
        if self._retiring:
            return
        self._retiring = True
        logger.info("MCP bridge: addon copy replaced or removed, stopping")
        GLib.idle_add(lambda: self.controller.stop() and False)

    def service_actions(self):
        # serve_forever calls this about twice a second, so an orphaned
        # server stops soon after an uninstall or upgrade.
        if not self._retiring and not is_current_copy():
            self.retire()


# ---------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------


class BridgeController:
    """
    Starts and stops the MCP server and owns its settings. All methods
    must be called on the GTK main thread. ``changed`` fires whenever
    the running state, error or settings change.
    """

    def __init__(self):
        self.changed = Signal()
        self.error: str | None = None
        self._server: _Server | None = None

    @property
    def settings(self):
        return get_settings()

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> bool:
        if self._server is not None:
            return True
        settings = self.settings
        self.error = settings.problem()
        if self.error is None:
            self._retire_previous_copies(scan=False)
            try:
                self._server = self._bind(settings)
            except OSError as e:
                self.error = _("Cannot listen on port {port}: {error}").format(
                    port=settings.port, error=e.strerror or e
                )
        if self._server is not None:
            setattr(get_context(), CONTROLLER_SLOT, self)
        else:
            logger.error(f"MCP bridge not started: {self.error}")
            tools.notify(
                _("MCP server not started: {error}").format(error=self.error)
            )
            self.changed.send(self)
            return False

        threading.Thread(
            target=self._server.serve_forever,
            name="mcp-bridge",
            daemon=True,
        ).start()
        where = settings.url
        if settings.allow_lan:
            where += " " + _("(also reachable from the network)")
        logger.info(f"MCP bridge listening on {settings.host}:{settings.port}")
        tools.notify(_("MCP server running at {url}").format(url=where))
        self.changed.send(self)
        return True

    def _bind(self, settings) -> "_Server":
        address = (settings.host, settings.port)
        try:
            return _Server(address, Handler, self)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            if not self._retire_previous_copies(scan=True):
                raise
        return _Server(address, Handler, self)

    def _retire_previous_copies(self, scan: bool) -> bool:
        """
        Stop servers still run by earlier copies of this addon: an
        upgrade or reinstall loads a new copy without unloading the old
        one. The slot finds copies from 0.3.1 on; with ``scan``, older
        copies are found among live objects. Returns True if any server
        was stopped.
        """
        others = []
        previous = getattr(get_context(), CONTROLLER_SLOT, None)
        if previous is not None and previous is not self:
            others.append(previous)
        if scan:
            for obj in gc.get_objects():
                cls = type(obj)
                if (
                    cls.__name__ == "BridgeController"
                    and cls.__module__ == __name__
                    and obj is not self
                    and all(obj is not other for other in others)
                ):
                    others.append(obj)
        stopped = False
        for other in others:
            if getattr(other, "_server", None) is not None:
                logger.info("MCP bridge: stopping a previous copy's server")
                other.stop()
                stopped = True
        return stopped

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        tools.cancel_pending_confirmation()
        server.shutdown()
        server.server_close()
        logger.info("MCP bridge stopped")
        tools.notify(_("MCP server stopped"))
        self.changed.send(self)

    def update_settings(self, **changes) -> None:
        settings = self.settings
        changed = {
            key: value
            for key, value in changes.items()
            if getattr(settings, key) != value
        }
        if not changed:
            return
        for key, value in changed.items():
            setattr(settings, key, value)
        save_settings(settings)
        if self.running and RESTART_KEYS & changed.keys():
            self.stop()
            self.start()
        else:
            self.changed.send(self)


_controller: BridgeController | None = None


def get_controller() -> BridgeController:
    global _controller
    if _controller is None:
        _controller = BridgeController()
    return _controller
