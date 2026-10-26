"""
MCP over Streamable HTTP (JSON responses, no server-initiated stream),
and the controller that starts and stops the server.
"""

import hmac
import json
import logging
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

Before the first machine action on a machine, call machine_info and \
tell the user about any "checks" it reports (e.g. GRBL laser mode off).

Machine actions (home, jog, frame, start, resume, clear alarm) wait for \
the operator to click Allow in Rayforge. If one is denied or times out, \
do not retry it in a loop: tell the user. machine_stop never needs \
approval; use it if anything looks wrong.
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
        if ADDON_NAME not in get_context().addon_mgr.loaded_addons:
            # Uninstalling unloads the modules without calling
            # on_unload, so the server notices on the next request.
            message = "The MCP Bridge addon was removed from Rayforge."
            self._send_json(503, rpc_error(None, -32002, message))
            GLib.idle_add(lambda: get_controller().stop() and False)
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
            try:
                self._server = _Server((settings.host, settings.port), Handler)
            except OSError as e:
                self.error = _("Cannot listen on port {port}: {error}").format(
                    port=settings.port, error=e.strerror or e
                )
        if self._server is None:
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
