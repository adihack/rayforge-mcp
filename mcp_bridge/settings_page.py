"""Settings dialog page for the MCP bridge."""

import secrets
from gettext import gettext as _

from gi.repository import Adw, Gdk, GLib, Gtk

from .server import get_controller
from .settings import MAX_PORT, MIN_PORT

PORT_APPLY_DELAY_MS = 800


def client_command(settings) -> str:
    command = f"claude mcp add --transport http rayforge {settings.url}"
    if settings.token:
        command += f' --header "Authorization: Bearer {settings.token}"'
    return command


class McpBridgeSettingsPage(Adw.PreferencesPage):
    """Server on/off, port, access token and machine-control options."""

    def __init__(self):
        super().__init__(
            title=_("MCP Bridge"), icon_name="network-server-symbolic"
        )
        self._controller = get_controller()
        self._syncing = False
        self._port_timeout_id = 0
        settings = self._controller.settings

        server_group = Adw.PreferencesGroup(
            title=_("MCP Server"),
            description=_(
                "Lets AI assistants such as Claude work with the open "
                "document. Machine actions always need your approval in "
                "Rayforge."
            ),
        )
        self.add(server_group)

        self.running_row = Adw.SwitchRow(title=_("Run Server"))
        self.running_row.connect("notify::active", self._on_running_toggled)
        server_group.add(self.running_row)

        self.autostart_row = Adw.SwitchRow(
            title=_("Start With Rayforge"), active=settings.auto_start
        )
        self.autostart_row.connect(
            "notify::active",
            lambda row, _p: self._update(auto_start=row.get_active()),
        )
        server_group.add(self.autostart_row)

        self.port_row = Adw.SpinRow.new_with_range(MIN_PORT, MAX_PORT, 1)
        self.port_row.set_title(_("Port"))
        self.port_row.set_value(settings.port)
        self.port_row.connect("notify::value", self._on_port_changed)
        server_group.add(self.port_row)

        access_group = Adw.PreferencesGroup(title=_("Access"))
        self.add(access_group)

        self.token_row = Adw.PasswordEntryRow(
            title=_("Access Token (optional)"), show_apply_button=True
        )
        self.token_row.set_text(settings.token)
        self.token_row.connect("apply", self._on_token_applied)
        generate = Gtk.Button(
            label=_("Generate"), valign=Gtk.Align.CENTER, css_classes=["flat"]
        )
        generate.connect("clicked", self._on_generate_token)
        self.token_row.add_suffix(generate)
        access_group.add(self.token_row)

        self.lan_row = Adw.SwitchRow(
            title=_("Allow Connections From Other Computers"),
            subtitle=_("Requires an access token"),
            active=settings.allow_lan,
        )
        self.lan_row.connect(
            "notify::active",
            lambda row, _p: self._update(allow_lan=row.get_active()),
        )
        access_group.add(self.lan_row)

        machine_group = Adw.PreferencesGroup(title=_("Machine Control"))
        self.add(machine_group)

        self.machine_row = Adw.SwitchRow(
            title=_("Allow Machine Control"),
            subtitle=_(
                "Start, frame, home and jog after your approval. Status, "
                "pause and stop are always available."
            ),
            active=settings.machine_tools,
        )
        self.machine_row.connect(
            "notify::active",
            lambda row, _p: self._update(machine_tools=row.get_active()),
        )
        machine_group.add(self.machine_row)

        self.timeout_row = Adw.SpinRow.new_with_range(10, 600, 5)
        self.timeout_row.set_title(_("Approval Timeout"))
        self.timeout_row.set_subtitle(
            _("Seconds before an unanswered request is denied")
        )
        self.timeout_row.set_value(settings.confirm_timeout)
        self.timeout_row.connect(
            "notify::value",
            lambda row, _p: self._update(confirm_timeout=int(row.get_value())),
        )
        machine_group.add(self.timeout_row)

        client_group = Adw.PreferencesGroup(
            title=_("Connect a Client"),
            description=_(
                "Run this in a terminal to add Rayforge to Claude Code."
            ),
        )
        self.add(client_group)

        self.command_row = Adw.ActionRow(
            title=_("Claude Code"), subtitle_selectable=True
        )
        copy = Gtk.Button(
            icon_name="edit-copy-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Copy"),
            css_classes=["flat"],
        )
        copy.connect("clicked", self._on_copy_command)
        self.command_row.add_suffix(copy)
        client_group.add(self.command_row)

        self._controller.changed.connect(self._on_controller_changed)
        self._sync()

    def _update(self, **changes):
        if not self._syncing:
            self._controller.update_settings(**changes)

    def _on_controller_changed(self, sender):
        self._sync()

    def _sync(self):
        controller = self._controller
        settings = controller.settings
        self._syncing = True
        try:
            self.running_row.set_active(controller.running)
            if controller.running:
                subtitle = _("Running at {url}").format(url=settings.url)
            elif controller.error:
                subtitle = controller.error
            else:
                subtitle = _("Stopped")
            self.running_row.set_subtitle(subtitle)
            self.lan_row.set_active(settings.allow_lan)
            self.machine_row.set_active(settings.machine_tools)
            self.command_row.set_subtitle(
                GLib.markup_escape_text(client_command(settings))
            )
        finally:
            self._syncing = False

    def _on_running_toggled(self, row, _pspec):
        if self._syncing:
            return
        if row.get_active():
            self._controller.start()
        else:
            self._controller.stop()
        self._sync()

    def _on_port_changed(self, row, _pspec):
        if self._port_timeout_id:
            GLib.source_remove(self._port_timeout_id)
        self._port_timeout_id = GLib.timeout_add(
            PORT_APPLY_DELAY_MS, self._apply_port
        )

    def _apply_port(self):
        self._port_timeout_id = 0
        self._update(port=int(self.port_row.get_value()))
        return GLib.SOURCE_REMOVE

    def _on_token_applied(self, row):
        self._update(token=row.get_text().strip())

    def _on_generate_token(self, _button):
        token = secrets.token_urlsafe(24)
        self.token_row.set_text(token)
        self._update(token=token)

    def _on_copy_command(self, _button):
        text = client_command(self._controller.settings)
        clipboard = self.get_display().get_clipboard()
        clipboard.set_content(Gdk.ContentProvider.new_for_value(text))
