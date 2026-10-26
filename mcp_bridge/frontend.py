"""
Frontend hooks: a Tools menu toggle, a Settings page, and starting the
server once the main window exists.
"""

from gettext import gettext as _

from gi.repository import Gio, GLib

from rayforge.context import get_context
from rayforge.core.addon_config import AddonState
from rayforge.core.hooks import hookimpl
from rayforge.ui_gtk.action_registry import MenuPlacement, action_registry
from rayforge.ui_gtk.actions import action_extension_registry

from . import ADDON_NAME, tools
from .server import get_controller
from .settings_page import McpBridgeSettingsPage

TOGGLE_ACTION = "mcp-bridge-toggle"

_toggle_action: Gio.SimpleAction | None = None


def _auto_start():
    controller = get_controller()
    if controller.settings.auto_start and not controller.running:
        controller.start()


def _setup_actions(action_manager):
    """
    Rayforge calls this while building the window, and again whenever
    any addon is enabled or disabled, so it must be idempotent.
    """
    global _toggle_action
    win = action_manager.win
    tools.bind_window(win)
    if (
        _toggle_action is not None
        and win.lookup_action(TOGGLE_ACTION) is _toggle_action
    ):
        return

    controller = get_controller()
    action = Gio.SimpleAction.new_stateful(
        TOGGLE_ACTION, None, GLib.Variant.new_boolean(controller.running)
    )

    def on_change_state(_action, value):
        if value.get_boolean():
            controller.start()
        else:
            controller.stop()

    def on_controller_changed(_sender):
        action.set_state(GLib.Variant.new_boolean(controller.running))

    action.connect("change-state", on_change_state)
    controller.changed.connect(on_controller_changed, weak=False)
    win.add_action(action)
    action_manager.actions[TOGGLE_ACTION] = action
    action_registry.register(
        action_name=TOGGLE_ACTION,
        action=action,
        addon_name=ADDON_NAME,
        label=_("MCP Server for AI Assistants"),
        menu=MenuPlacement(menu_id="tools", priority=90),
    )
    _toggle_action = action

    # Enabled from the Addon Manager while Rayforge is running:
    # main_window_ready has already fired, so start from here.
    if win.get_realized():
        _auto_start()


def _attach_if_installed_at_runtime():
    """
    Installing from the Addon Manager loads this module into a running
    Rayforge without calling register_actions or main_window_ready, so
    attach to the existing window here. At normal startup the window
    does not exist yet (or setup already ran), and this does nothing.
    """
    if _toggle_action is not None:
        return GLib.SOURCE_REMOVE
    app = Gio.Application.get_default()
    win = app.get_active_window() if app else None
    action_manager = getattr(win, "action_manager", None)
    if action_manager is not None:
        action_extension_registry.register_setup(_setup_actions, ADDON_NAME)
        _setup_actions(action_manager)
    return GLib.SOURCE_REMOVE


GLib.idle_add(_attach_if_installed_at_runtime)


@hookimpl
def register_actions(action_registry):
    action_extension_registry.register_setup(_setup_actions, ADDON_NAME)


@hookimpl
def register_settings_pages(settings_page_registry):
    settings_page_registry.register(
        McpBridgeSettingsPage, addon_name=ADDON_NAME
    )


@hookimpl
def main_window_ready(main_window):
    tools.bind_window(main_window)
    _auto_start()


@hookimpl
def on_unload():
    # Rayforge calls on_unload on every addon when any one is disabled;
    # by then the one being unloaded is already marked disabled.
    state = get_context().addon_config.get_state(ADDON_NAME)
    if state != AddonState.DISABLED:
        return
    get_controller().stop()
    tools.bind_window(None)
