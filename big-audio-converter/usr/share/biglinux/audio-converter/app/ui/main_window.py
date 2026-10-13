"""The main window: header, settings sidebar, queue and playback panel."""

import gettext
import logging
import os
import weakref
from copy import deepcopy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from big_gtk_kit.hover_cards import CardLayer
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from app.audio.waveform import WaveformGenerator
from app.main_loop import MainLoopSources, weak_callback
from app.ui.batch_runner import BatchRunner
from app.ui.conversion_settings import COMPRESSOR_STEPS, ConversionSettingsMixin
from app.ui.effect_cards import EffectCards
from app.ui.equalizer_panel import EqualizerPanel
from app.ui.file_queue import FileQueue
from app.ui.help_tooltips import HelpTooltips
from app.ui.playback import PlaybackMixin
from app.ui.playback_panel import PlaybackPanelMixin
from app.ui.segment_editor import SegmentEditor
from app.ui.time_formatter import format_time_display
from app.ui.visualizer import AudioVisualizer

_ = gettext.gettext
logger = logging.getLogger(__name__)


class MainWindow(
    ConversionSettingsMixin,
    PlaybackMixin,
    PlaybackPanelMixin,
    Adw.ApplicationWindow,
):
    """Main application window."""

    def __init__(self, **kwargs):
        default_width = 1100
        default_height = 800
        config = kwargs["application"].config

        saved_width = config.get("window_width")
        if saved_width:
            try:
                default_width = int(saved_width)
            except (ValueError, TypeError):
                pass

        saved_height = config.get("window_height")
        if saved_height:
            try:
                default_height = max(int(saved_height), 600)
            except (ValueError, TypeError):
                pass

        is_maximized = str(config.get("window_maximized")).lower() == "true"

        super().__init__(
            title="Big Audio Converter",
            default_width=default_width,
            default_height=default_height,
            **kwargs,
        )

        self.add_css_class("big-audio-converter")

        self._should_maximize = is_maximized

        # The narrowest layout: the sidebar over the queue, the playback
        # controls on two lines.
        self.set_size_request(440, 480)

        self.app = kwargs.get("application")
        self._closed = False
        self._dialog_cancellable = Gio.Cancellable()
        self._probe_errors = []
        self._probe_error_dialog = None
        self._sources = MainLoopSources()
        self._playback_sources = MainLoopSources()
        self.waveform_generator = WaveformGenerator()

        self.help_tooltips = HelpTooltips(self.app.config)

        self.player = self.app.player
        self.converter = self.app.converter
        self.file_markers = {}  # Dictionary mapping file path to marker pairs
        self.active_audio_id = None  # Queue entry whose waveform is shown

        self._playing_selection = False
        self._selection_segments = []  # List of (start, stop) tuples
        self._current_segment_index = 0
        self._play_selection_mode = False
        self._marker_dragging = False
        self._is_transitioning_segment = False  # a seek between parts is pending
        self._syncing_volume = False
        self._syncing_speed = False

        self.visualizer_height = 132

        saved_height = config.get("visualizer_height")
        if saved_height:
            try:
                self.visualizer_height = max(100, int(saved_height))
            except (ValueError, TypeError):
                pass

        self.setup_ui()
        self.conversion = BatchRunner(self)
        self.file_queue.on_probe_error = self._on_probe_error
        owner = weakref.proxy(self)
        self.file_queue.on_trash_error = lambda title, body: owner._show_message(
            title, body
        )
        self.file_queue.on_pending_changed = self._on_pending_changed
        self.setup_drop_target()
        self.connect("close-request", weak_callback(self.on_close_request))
        if self.app.config.load_warning:
            # The unreadable file is kept until a setting is changed.
            self.toast_overlay.add_toast(
                Adw.Toast(title=_("Settings could not be read and were reset"))
            )

        self.connect("map", weak_callback(self.on_window_mapped))

        self.connect("notify::maximized", weak_callback(self._on_window_state_changed))

        self.player.connect(
            "position-updated", weak_callback(self.on_player_position_updated)
        )
        self.player.connect(
            "duration-changed", weak_callback(self.on_player_duration_changed)
        )
        self.player.connect(
            "state-changed", weak_callback(self.on_player_state_changed)
        )
        self.player.connect("eos", weak_callback(self.on_playback_finished))
        self.player.connect("error", weak_callback(self._on_player_error))

        # Restore maximized state after window is fully initialized
        if self._should_maximize:
            self._sources.later(100, self.maximize)

        self.file_queue.connect_file_removed_signal(self._on_file_removed)

        self._setup_keyboard_shortcuts()

    def _setup_keyboard_shortcuts(self):
        """Register window-level keyboard shortcuts."""
        owner = weakref.proxy(self)
        add_files_action = Gio.SimpleAction.new("add-files", None)
        add_files_action.connect("activate", lambda *_: owner.on_add_files(None))
        self.add_action(add_files_action)

        convert_action = Gio.SimpleAction.new("convert", None)
        convert_action.connect("activate", lambda *_: owner.on_convert(None))
        self.add_action(convert_action)

        cancel = Gio.SimpleAction.new("cancel-conversion", None)
        cancel.connect("activate", lambda *_: owner.conversion.cancel())
        self.add_action(cancel)

        clear = Gio.SimpleAction.new("clear-queue", None)
        clear.set_enabled(False)
        clear.connect("activate", lambda *_: owner.on_clear_queue())
        self.add_action(clear)

        play_pause_action = Gio.SimpleAction.new("play-pause", None)
        play_pause_action.connect(
            "activate", lambda *_: owner._on_pause_play_clicked(None)
        )
        self.add_action(play_pause_action)

        reset = Gio.SimpleAction.new("reset-settings", None)
        reset.connect("activate", lambda *_: owner._confirm_reset_settings())
        self.add_action(reset)

        edit = Gio.SimpleAction.new("edit-segments", None)
        edit.connect("activate", lambda *_: owner.on_edit_segments())
        self.add_action(edit)

        app = self.get_application()
        app.set_accels_for_action("win.add-files", ["<Control>o"])
        app.set_accels_for_action("win.convert", ["<Control>Return"])
        app.set_accels_for_action("win.play-pause", ["<Control>space"])
        app.set_accels_for_action("win.edit-segments", ["<Control>e"])

    def setup_ui(self):
        owner = weakref.proxy(self)
        self.vertical_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        # Shown over the whole window while files are dragged in.
        self.drop_overlay = Adw.StatusPage(
            icon_name="list-add-symbolic",
            title=_("Drop to add"),
            css_classes=["drop-overlay"],
            can_target=False,
            visible=False,
        )
        self.toast_overlay = Adw.ToastOverlay(
            child=Gtk.Overlay(child=self.vertical_paned)
        )
        self.toast_overlay.get_child().add_overlay(self.drop_overlay)
        # The settings' and the playback bar's cards open above everything.
        self.card_layer = CardLayer()
        self.toast_overlay.get_child().add_overlay(self.card_layer)
        self.set_content(self.toast_overlay)
        self.vertical_paned.set_vexpand(True)

        # Prevent bottom controls from being cut off when window is resized
        self.vertical_paned.set_shrink_end_child(False)
        self.vertical_paned.set_resize_start_child(True)
        self.vertical_paned.set_resize_end_child(False)

        self.split_view = Adw.OverlaySplitView(vexpand=True)
        self.split_view.set_sidebar_position(Gtk.PackType.START)
        self.split_view.set_min_sidebar_width(280)
        # Wide enough that row subtitles keep to one line.
        self.split_view.set_max_sidebar_width(400)
        self.split_view.set_sidebar_width_fraction(0.34)
        self.vertical_paned.set_start_child(self.split_view)

        # Left: what the files become.
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_vexpand(True)
        left_scroll.add_css_class("sidebar")

        left_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_bottom=12)
        left_scroll.set_child(left_content)
        self.effect_cards = EffectCards(left_scroll, self.card_layer)
        # The pause panel in the sidebar previews on the waveform.
        self.visualizer = AudioVisualizer()
        self.visualizer.player = self.player
        self.setup_conversion_options(left_content)
        self.options_scroll = left_scroll
        # The window buttons stay on the queue's header.
        left_header = Adw.HeaderBar(
            css_classes=["sidebar"],
            decoration_layout="",
            title_widget=Adw.WindowTitle(title="Big Audio Converter"),
        )
        left_box = Adw.ToolbarView(content=left_scroll)
        left_box.add_top_bar(left_header)

        # Right: the queue, with adding and converting in the header's middle.
        right_box = Adw.ToolbarView(css_classes=["queue-pane"])
        right_box.set_size_request(320, -1)
        queue_header = Adw.HeaderBar()
        queue_header.set_decoration_layout(":minimize,maximize,close")
        # Shown once the sidebar folds away; it names how many adjustments
        # the files will get, where there is room for it.
        self.adjustments_label = Gtk.Label(margin_start=6, visible=False)
        self.adjustments_slot = Gtk.Box()
        self.adjustments_slot.append(self.adjustments_label)
        sidebar_content = Gtk.Box()
        sidebar_content.append(Gtk.Image(icon_name="sidebar-show-symbolic"))
        sidebar_content.append(self.adjustments_slot)
        self.sidebar_button = Gtk.ToggleButton(
            child=sidebar_content, visible=False, tooltip_text=_("Show settings")
        )
        self.sidebar_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Show settings")]
        )
        self.split_view.bind_property(
            "show-sidebar",
            self.sidebar_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        queue_header.pack_start(self.sidebar_button)
        # Named in words: the rows' own remove buttons use an icon. Both
        # leave the header when the window narrows; the menu clears too.
        self.queue_info = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.clear_queue_button = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="edit-clear-all-symbolic", label=_("Clear queue")
            ),
            action_name="win.clear-queue",
            css_classes=["flat"],
            visible=False,
        )
        self.clear_queue_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Clear queue")]
        )
        self.queue_info.append(self.clear_queue_button)
        self.queue_label = Gtk.Label(
            css_classes=["caption", "dim-label"],
            margin_start=4,
            margin_end=8,
            visible=False,
        )
        self.queue_info.append(self.queue_label)
        queue_header.pack_start(self.queue_info)

        # In the narrowest window adding shows as a plus.
        self.add_files_face = Gtk.Stack(hhomogeneous=False, interpolate_size=True)
        self.add_files_face.add_named(Gtk.Label(label=_("Add Files")), "label")
        self.add_files_face.add_named(Gtk.Image(icon_name="list-add-symbolic"), "icon")
        self.add_files_button = Gtk.Button(
            child=self.add_files_face,
            action_name="win.add-files",
            css_classes=["suggested-action"],
        )
        self.add_files_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Add Files")]
        )
        self.convert_button = Gtk.Button(
            action_name="win.convert", css_classes=["suggested-action"], visible=False
        )
        center = Gtk.Box(spacing=8)
        center.append(self.add_files_button)
        center.append(self.convert_button)
        queue_header.set_title_widget(center)

        menu = Gio.Menu()
        menu.append_section(_("Playback"), self.playback_menu_section())
        options_section = Gio.Menu()
        options_section.append(_("Show help on hover"), "app.toggle-tips")
        options_section.append(_("Delete Without Using the Trash"), "app.skip-trash")
        menu.append_section(None, options_section)
        queue_section = Gio.Menu()
        queue_section.append(_("Clear Queue"), "win.clear-queue")
        queue_section.append(_("Reset All Settings…"), "win.reset-settings")
        menu.append_section(None, queue_section)
        about_section = Gio.Menu()
        about_section.append(_("About"), "app.about")
        menu.append_section(None, about_section)
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu, primary=True
        )
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Main menu")])
        # Outputs are looked up again each time the menu opens.
        menu_button.get_popover().connect(
            "notify::visible",
            lambda popover, _pspec: (
                owner.player.refresh_audio_devices() if popover.get_visible() else None
            ),
        )
        queue_header.pack_end(menu_button)
        right_box.add_top_bar(queue_header)
        # A finished batch: what happened, and the next step.
        self.banner = Adw.Banner()
        self.banner.connect(
            "button-clicked", lambda *_: owner.conversion.banner_activated()
        )
        right_box.add_top_bar(self.banner)

        self.file_queue = FileQueue(self.converter, self.file_markers)

        self.file_queue.on_queue_size_changed = self._show_queue_summary

        self.file_queue.on_play_file = weak_callback(self.on_play_file)

        self.file_queue.on_stop_playback = self.player.stop

        self.file_queue.on_file_added_to_empty_queue = self.on_file_added_to_empty_queue

        self.file_queue.on_activate_file = self.on_activate_file

        right_box.set_content(self.file_queue)

        self.eq_panel = EqualizerPanel(self.player, self.app.config)
        self.eq_panel.on_changed = weak_callback(self._show_equalizer_state)
        self.eq_revealer = Gtk.Revealer()
        self.eq_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.eq_revealer.set_transition_duration(200)
        self.eq_revealer.set_reveal_child(False)
        self.eq_revealer.set_child(self.eq_panel)
        right_box.add_bottom_bar(self.eq_revealer)
        right_box.add_bottom_bar(self.destination_bar)

        self.split_view.set_sidebar(left_box)
        self.split_view.set_content(right_box)

        self.setup_playback_panel()

        # The player arrives with the first file; cut rows with a segment.
        self.visualizer_container.set_visible(False)
        self._update_cut_rows()

        # Narrow windows fold the sidebar over the queue; narrower ones also
        # stack the playback controls. One breakpoint applies at a time, so
        # the narrower repeats the wider one's changes.
        # Secondary controls leave first; their actions stay reachable (the
        # wheel zooms, the menu clears, the Convert label counts).
        compact = [
            (self.zoom_anchor, "visible", False),
            (self.selection_content, "label", ""),
            (self.queue_info, "visible", False),
        ]
        folded = [
            (self.split_view, "collapsed", True),
            (self.sidebar_button, "visible", True),
        ]
        stacked = [
            (self.controls_layout, "layout-name", "narrow"),
            (self.add_files_face, "visible-child-name", "icon"),
            (self.adjustments_slot, "visible", False),
        ]
        for width, setters in (
            (1080, compact),
            (860, compact + folded),
            (830, compact + folded + stacked),
        ):
            breakpoint = Adw.Breakpoint.new(
                Adw.BreakpointCondition.parse(f"max-width: {width}sp")
            )
            for widget, name, value in setters:
                breakpoint.add_setter(widget, name, value)
            self.add_breakpoint(breakpoint)

        self._show_equalizer_state()

        # Apply tooltips to all UI elements (must be after all widgets are created)
        self._apply_tooltips()
        self._restore_conversion_settings()
        self.show_delete_mode()
        self._on_format_changed(self.format_row, None)

    def _show_queue_summary(self, count):
        """The queue's size and length, and the controls that need files."""
        if not self.converter.busy:  # a conversion shows its progress there
            summary = gettext.ngettext("{count} file", "{count} files", count).format(
                count=count
            )
            total = self.file_queue.total_duration()
            if total:
                summary += " · " + format_time_display(total)
            self.queue_label.set_label(summary)
            self.queue_label.set_visible(count > 1)
            self.clear_queue_button.set_visible(count > 1)
        self.banner.set_revealed(False)
        self.lookup_action("clear-queue").set_enabled(count > 0)
        self._sync_convert_action()
        self.prev_audio_btn.set_visible(count > 1)
        self.next_audio_btn.set_visible(count > 1)
        self.convert_button.set_visible(count > 0)
        self._sync_join()
        self._update_convert_label()
        self._set_player_visible(count > 0)

    def _sync_convert_action(self):
        queue = self.file_queue
        self.lookup_action("convert").set_enabled(
            bool(queue.files) and not queue.pending and not self.converter.busy
        )

    def _update_convert_label(self):
        count = len(self.file_queue.files)
        if count > 1 and self._joining():
            label = gettext.ngettext("Join %d file", "Join %d files", count)
        else:
            label = gettext.ngettext("Convert %d file", "Convert %d files", count)
        self.convert_button.set_label(label % count)

    def _apply_tooltips(self):
        """Register every help tooltip; the helper shows them only when enabled."""
        for widget, key in (
            (self.prev_audio_btn, "prev_audio_btn"),
            (self.pause_play_btn, "pause_play_btn"),
            (self.next_audio_btn, "next_audio_btn"),
            (self.play_selection_switch, "play_selection_switch"),
            (self.segment_edit_button, "segment_edit_button"),
            (self.visualizer, "waveform_visualizer"),
        ):
            self.help_tooltips.add_tooltip(widget, key)
        self.show_help()

    def show_help(self):
        """Tooltips and the waveform's first hint follow the menu's choice."""
        self.visualizer.show_hint = self.help_tooltips.refresh()
        self.visualizer.queue_draw()

    def on_convert(self, button):
        """Delegate job presentation to the conversion controller."""
        self.conversion.start()

    def _collect_conversion_settings(self):
        """Collect an immutable request before the worker starts."""
        self._save_current_file_state()
        settings = {
            "format": self._format_list[self.format_row.get_selected()],
            "bitrate": self._bitrate_list[self.bitrate_row.get_selected()],
            "volume": self.volume_spin.get_value() / 100,
            "speed": self.speed_spin.get_value(),
            # Channels: 0=original, 1=mono, 2=stereo
            "channels": (None, 1, 2)[self.channels_row.get_selected()],
            "sample_rate": self._sample_rate_list[self.sample_rate_row.get_selected()],
            "allow_precision_reduction": self.precision_row.get_active(),
            "bitrate_per_channel": self.bitrate_channels_row.get_active(),
            "output_directory": self.output_directory,
            "delete_originals": self.delete_originals_switch.get_active(),
            "skip_trash": self._config_bool("skip_trash", False),
            "noise_reduction": self._noise_active(),
            "noise_engine": self._noise_engine(),
            "noise_strength": self.noise_adjustment.get_value(),
            "voice_only": self.voice_only_switch.get_active(),
            "voice_only_sensitivity": round(self.voice_only_scale.get_value()),
            # Each correction is off at its first step.
            "compressor_enabled": self.compressor_row.steps.get_selected() > 0,
            "compressor_intensity": COMPRESSOR_STEPS[
                self.compressor_row.steps.get_selected()
            ],
            "eq_enabled": any(
                self.eq_panel.band_scales[f].get_value() != 0
                for _, f in self.eq_panel.BANDS
            ),
            "eq_bands": ",".join(
                str(self.eq_panel.band_scales[f].get_value())
                for _, f in self.eq_panel.BANDS
            ),
            "normalize": self.normalize_row.get_active(),
            "cut_merge": self.cut_output_row.get_selected() == 1,
            "cut_remove": self.removes_marked(),
            "join_files": self._joining(),
            # The converter orders each file's segments by number or by time.
            "order_by_segment_number": self.cut_order_row.get_selected() == 1,
            # Files without an entry here are converted in full.
            "file_markers": self.file_markers,
            # Pass track metadata from file queue for video track extraction
            "track_metadata": self.file_queue.track_metadata,
        }

        if settings["format"] == "copy":
            for key in (
                "noise_reduction",
                "compressor_enabled",
                "eq_enabled",
                "normalize",
            ):
                settings[key] = False
            settings.update(
                volume=1.0, speed=1.0, channels=None, sample_rate="original"
            )
        return deepcopy(settings)

    def _show_message(self, title, message):
        if self._closed:
            return
        dialog = Adw.AlertDialog(heading=title, body=message)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def on_close_request(self, window):
        if not self._closed and self.converter.busy:
            dialog = Adw.AlertDialog(
                heading=_("Stop the conversion?"),
                body=_(
                    "Closing the window stops the conversion in progress. "
                    "Files that already finished are kept."
                ),
            )
            dialog.add_response("keep", _("Keep Converting"))
            dialog.add_response("stop", _("Stop and Close"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("keep")
            dialog.set_close_response("keep")
            dialog.connect("response", weak_callback(self._on_close_while_converting))
            dialog.present(self)
            return True
        self.cleanup()
        return False

    def _on_close_while_converting(self, _dialog, response):
        if response == "stop" and not self._closed:
            self.cleanup()
            self.close()

    def cleanup(self):
        """The same shutdown path is used by window close, Ctrl+Q, and app quit."""
        if self._closed:
            return
        self._closed = True
        self._dialog_cancellable.cancel()
        if not self.is_maximized():
            self._save_window_size()
        self._remember_waveform_height()
        self._sources.close()
        self._playback_sources.close()
        self.conversion.cleanup()
        self.waveform_generator.cancel()
        self.pause_panel.cleanup()
        self.effect_cards.cleanup()
        self.bar_cards.unparent_all()
        self.converter.cancel_conversion()
        self.file_queue.cleanup()
        self.waveform_generator.cleanup()
        # The player and converter belong to the application, which disposes
        # of them at shutdown: a window opened meanwhile still needs them.
        self.player.stop()
        self.visualizer.cleanup()
        self.help_tooltips.cleanup()
        self.seekbar.connect_seek_handler(None)
        if self.app._main_window is self:
            self.app._main_window = None
        self.app.config.flush()

    def on_edit_segments(self, *_args):
        if not self.active_audio_id:
            self._show_message(
                _("No audio selected"),
                _("Add a file and select it before editing segments."),
            )
            return
        if (self.visualizer.duration or self.player.duration) <= 0:
            self._show_message(
                _("Audio duration unavailable"),
                _("Wait for the waveform to finish loading before editing this file."),
            )
            return
        editor = SegmentEditor(self)
        self.segment_editor = editor
        owner = weakref.proxy(self)
        editor.connect("closed", lambda dialog: setattr(owner, "segment_editor", None))
        editor.present(self)

    def _request_waveform(self, file_path):
        if self._closed or file_path not in self.file_queue.files:
            return
        self.pause_panel.sync()
        self.waveform_generator.request(
            file_path,
            self.converter,
            self.visualizer,
            self.file_markers,
            track_metadata=self.file_queue.track_metadata,
        )

    def _on_pending_changed(self, pending):
        if not self._closed:
            self._sync_convert_action()

    def _on_probe_error(self, path, message):
        if self._closed:
            return
        self._probe_errors.append((os.path.basename(path), message))
        if len(self._probe_errors) > 100:
            self._probe_errors.pop(0)
        self._sources.later(150, self._show_probe_errors, key="probe-errors")

    def _show_probe_errors(self):
        if self._closed or not self._probe_errors:
            return
        body = "\n\n".join(
            name + "\n" + message for name, message in self._probe_errors[-5:]
        )
        body += "\n\n" + _(
            "Other valid files remain in the queue. Check these files and add them again."
        )
        if self._probe_error_dialog is None:
            self._probe_error_dialog = Adw.AlertDialog(
                heading=_("Some files could not be added"), body=body
            )
            self._probe_error_dialog.add_response("ok", _("OK"))
            self._probe_error_dialog.connect(
                "closed", weak_callback(self._probe_errors_closed)
            )
            self._probe_error_dialog.present(self)
        else:
            self._probe_error_dialog.set_body(body)

    def _probe_errors_closed(self, _dialog):
        self._probe_error_dialog = None
        self._probe_errors.clear()

    def _on_player_error(self, message):
        if not self._closed:
            self._show_message(_("Audio preview"), message)

    def _on_window_state_changed(self, window, param):
        """Save the maximized state and fix the layout after unmaximizing."""
        is_maximized = self.is_maximized()
        self.app.config.set("window_maximized", str(is_maximized).lower())
        if not is_maximized:
            # A small delay lets the window settle before measuring it.
            self._sources.later(200, self._restore_geometry)

    def _save_window_size(self):
        width = self.get_width()
        height = self.get_height()
        if width > 200 and height > 200:
            self.app.config.set("window_width", str(width))
            self.app.config.set("window_height", str(height))

    def on_window_mapped(self, widget):
        """Restore geometry once the window has its first allocation."""
        self._sources.idle(self._restore_geometry)

    def _restore_geometry(self):
        """Restore the visualizer height after the window is shown."""
        self._fit_bottom_panel()

    def _confirm_reset_settings(self):
        dialog = Adw.AlertDialog(
            heading=_("Reset all settings?"),
            body=_(
                "Format, quality, output folder, deleting originals, cutting, "
                "effects and equalizer return to their defaults. The queue and "
                "your source files are kept."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", weak_callback(self._on_reset_settings_response))
        dialog.present(self)

    def _on_reset_settings_response(self, _dialog, response):
        if response == "reset" and not self._closed:
            self.reset_settings()

    def on_clear_queue(self):
        """Cuts are work; a queue without them is cleared at once."""
        self._save_current_file_state()
        if not self.file_markers:
            self._on_clear_queue_response(None, "clear")
            return
        dialog = Adw.AlertDialog(
            heading=_("Clear Queue"),
            body=_(
                "Remove the queue entries and their edits? Source files will not be deleted."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("clear", _("Clear Queue"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", weak_callback(self._on_clear_queue_response))
        dialog.present(self)

    def _on_clear_queue_response(self, dialog, response):
        if not self._closed and response == "clear":
            if self.player and self.player.is_playing():
                self.player.stop()

            self.visualizer.clear_waveform()

            self.visualizer.clear_all_markers()

            self.file_queue.clear_queue()

    def setup_drop_target(self):
        """Files dropped anywhere in the window join the queue."""
        owner = weakref.proxy(self)
        overlay = self.drop_overlay

        def entered(*_args):
            if owner.converter.busy:
                return 0
            overlay.set_visible(True)
            return Gdk.DragAction.COPY

        def dropped(_target, value, _x, _y):
            overlay.set_visible(False)
            return not owner.converter.busy and owner.file_queue.add_dropped(value)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("enter", entered)
        drop_target.connect("leave", lambda *_: overlay.set_visible(False))
        drop_target.connect("drop", dropped)
        self.add_controller(drop_target)

    def on_add_files(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Select Audio or Video Files"))

        media_filter = Gtk.FileFilter()
        media_filter.set_name(_("Audio and Video files"))

        audio_extensions = [
            "mp3",
            "wav",
            "ogg",
            "flac",
            "m4a",
            "aac",
            "opus",
            "wma",
            "aiff",
            "ape",
            "alac",
            "dsd",
            "dsf",
            "mka",
            "oga",
            "spx",
            "tta",
            "wv",
        ]

        video_extensions = [
            "mp4",
            "mkv",
            "avi",
            "mov",
            "wmv",
            "flv",
            "webm",
            "m4v",
            "mpg",
            "mpeg",
            "3gp",
            "ogv",
            "ts",
            "mts",
            "m2ts",
        ]

        for ext in audio_extensions + video_extensions:
            media_filter.add_suffix(ext)
            media_filter.add_suffix(ext.upper())  # Also add uppercase versions

        all_filter = Gtk.FileFilter()
        all_filter.set_name(_("All files"))
        all_filter.add_pattern("*")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media_filter)
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(media_filter)

        # Open the dialog with multiple file selection
        dialog.open_multiple(
            parent=self,
            cancellable=self._dialog_cancellable,
            callback=self._on_open_files_complete,
        )

    def _on_open_files_complete(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
            if self._closed or files is None:
                return
            paths = [file.get_path() for file in files if file.get_path()]
            self.file_queue.add_files(paths)
            if len(paths) != files.get_n_items():
                self._show_message(
                    _("Local files only"),
                    _(
                        "Some selected files are remote. Download them before adding them."
                    ),
                )
        except GLib.Error as error:
            if (
                not self._closed
                and not error.matches(
                    Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED
                )
                and not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
            ):
                self._show_message(_("Files could not be selected"), error.message)

    def on_file_added_to_empty_queue(self, file_path, index):
        """Show the first file added to an empty queue without playing it."""
        if self.visualizer.waveform_data is None:
            self._prepare_visualizer_for_new_file(file_path, index)
