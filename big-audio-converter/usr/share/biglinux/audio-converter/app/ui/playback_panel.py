"""The window's playback panel: the waveform, the seek bar and the controls bar."""

import gettext
import math
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from big_gtk_kit.hover_cards import HoverCards
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from app.main_loop import weak_callback
from app.ui.effect_cards import ChoiceTiles
from app.ui.seek_bar import SeekBar

# Volume percentages a drag settles on, and how near, in slider units (of 100).
VOLUME_MAGNETS = (50, 100, 200, 300, 500)
MAGNET_REACH = 2.5

_ = gettext.gettext


class PlaybackPanelMixin:
    """Mixin building the playback panel of MainWindow and keeping it fitted."""

    def setup_playback_panel(self):
        owner = weakref.proxy(self)
        visualizer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        visualizer_container.add_css_class("playback-panel")

        # As in the BigLinux players: volume, speed, voice and what plays on the
        # left, the transport in the middle, the preview and waveform tools on
        # the right. Their sliders open as the pointer reaches them.
        self.bar_cards = HoverCards(self.card_layer, Gtk.PositionType.TOP)
        adjust = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.volume_button, self.volume_value, self.volume_scale = self._bar_control(
            adjust,
            "volume",
            "audio-volume-medium-symbolic",
            _("Volume"),
            self.volume_adjustment,
            (self.volume_spin,),
            magnets=[self._volume_to_slider(volume) for volume in VOLUME_MAGNETS],
        )
        self.speed_button, self.speed_value, _scale = self._bar_control(
            adjust,
            "speed",
            "power-profile-performance-symbolic",
            _("Speed"),
            self.speed_adjustment,
            (self.speed_spin,),
        )
        quality = Gtk.Label(
            label=_("Cleaning quality"), css_classes=["caption", "dim-label"]
        )
        self.voice_button, self.voice_value, self.voice_scale = self._bar_control(
            adjust,
            "voice",
            "audio-input-microphone-symbolic",
            _("Clean voice"),
            self.noise_adjustment,
            (quality, self.noise_model, self.voice_only_box, self.noise_status),
        )
        # Its label and slider span the card, as the tiles above do.
        self.voice_only_box.set_halign(Gtk.Align.FILL)
        self.voice_value.set_label("0")
        self._update_noise_availability()
        end_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)

        # The keyboard's way to the first mark, so it shows with any file. It
        # faces Selection only across the transport.
        self.segment_edit_button = Gtk.Button(
            label=_("Edit Segments"), css_classes=["flat"], valign=Gtk.Align.CENTER
        )
        self.segment_edit_button.connect(
            "clicked", weak_callback(self.on_edit_segments)
        )
        end_box.append(self.segment_edit_button)

        # Hear the same moment with and without the sidebar's adjustments.
        self.original_preview = Adw.ToggleGroup(
            valign=Gtk.Align.CENTER, visible=False, css_classes=["round"]
        )
        self.original_preview.add(Adw.Toggle(name="adjusted", label=_("Adjusted")))
        self.original_preview.add(Adw.Toggle(name="original", label=_("Original")))
        self.original_preview.set_active_name("adjusted")
        self.original_preview.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Listen with or without adjustments")]
        )
        self.original_preview.connect(
            "notify::active-name", lambda *_: owner._apply_effects_bypass()
        )
        end_box.append(self.original_preview)
        self.effect_cards.on_changed = weak_callback(self._update_preview_toggle)

        # Plays only the marked parts.
        self.selection_content = Adw.ButtonContent(
            icon_name="selection-mode-symbolic", label=_("Selection only")
        )
        self.play_selection_switch = Gtk.ToggleButton(child=self.selection_content)
        self.play_selection_switch.add_css_class("flat")
        self.play_selection_switch.set_active(False)
        self.play_selection_switch.connect(
            "toggled", weak_callback(self._on_play_selection_switch_toggled)
        )
        self.play_selection_switch.set_valign(Gtk.Align.CENTER)
        self.play_selection_switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Play only selected area")],
        )
        adjust.append(self.play_selection_switch)

        # As volume and speed: its readout opens a slider over the zoom, from
        # 1x to the waveform's most on a logarithmic scale.
        self.zoom_adjustment = Gtk.Adjustment(
            upper=100, step_increment=0.5, page_increment=10
        )
        self._zoom_from_waveform = False
        self.zoom_adjustment.connect(
            "value-changed", weak_callback(self._on_zoom_slider_changed)
        )
        self.zoom_reset_button = Gtk.Button(
            label=_("Reset zoom"), css_classes=["flat"], sensitive=False
        )
        self.zoom_reset_button.connect(
            "clicked", lambda _b: owner.visualizer.reset_zoom()
        )
        self.zoom_button, self.zoom_value, _scale = self._bar_control(
            end_box,
            "zoom",
            "system-search-symbolic",
            _("Zoom"),
            self.zoom_adjustment,
            (self.zoom_reset_button,),
        )
        self.zoom_value.set_label("1×")
        self.zoom_anchor = self.zoom_button.get_parent()

        # Bottom right, as in the players: it opens the equalizer above the
        # queue's foot and lights while the curve is not flat.
        self.equalizer_button = Gtk.ToggleButton(
            child=Gtk.Image(icon_name="big-audio-converter-equalizer-symbolic"),
            tooltip_text=_("Equalizer"),
            valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        self.equalizer_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Equalizer")]
        )
        self.eq_revealer.bind_property(
            "reveal-child",
            self.equalizer_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        end_box.append(self.equalizer_button)

        playback_controls_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.prev_audio_btn = Gtk.Button(
            icon_name="media-skip-backward-symbolic",
            css_classes=["flat", "circular"],
            visible=False,
        )
        self.prev_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Previous track")]
        )
        self.prev_audio_btn.connect(
            "clicked", lambda btn: owner._on_previous_audio_clicked()
        )
        playback_controls_box.append(self.prev_audio_btn)

        self.pause_play_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            css_classes=["circular", "suggested-action", "play-button"],
        )
        self.pause_play_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Play")])
        self.pause_play_btn.connect(
            "clicked", weak_callback(self._on_pause_play_clicked)
        )
        playback_controls_box.append(self.pause_play_btn)

        self.next_audio_btn = Gtk.Button(
            icon_name="media-skip-forward-symbolic",
            css_classes=["flat", "circular"],
            visible=False,
        )
        self.next_audio_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Next track")]
        )
        self.next_audio_btn.connect(
            "clicked", lambda btn: owner._on_next_audio_clicked()
        )
        playback_controls_box.append(self.next_audio_btn)

        # One set of controls, laid out on one line or, when narrow, on two.
        self.controls_layout = Adw.MultiLayoutView()
        for name, widget in (
            ("adjust", adjust),
            ("transport", playback_controls_box),
            ("tools", end_box),
        ):
            self.controls_layout.set_child(name, widget)
        wide = Gtk.CenterBox(
            css_classes=["playback-controls"],
            start_widget=Adw.LayoutSlot(id="adjust"),
            center_widget=Adw.LayoutSlot(id="transport"),
            end_widget=Adw.LayoutSlot(id="tools"),
        )
        narrow = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            css_classes=["playback-controls"],
        )
        narrow.append(
            Gtk.CenterBox(
                start_widget=Adw.LayoutSlot(id="adjust"),
                center_widget=Adw.LayoutSlot(id="transport"),
            )
        )
        narrow.append(Adw.LayoutSlot(id="tools", halign=Gtk.Align.CENTER))
        self.controls_layout.add_layout(Adw.Layout(name="wide", content=wide))
        self.controls_layout.add_layout(Adw.Layout(name="narrow", content=narrow))

        self.visualizer_container = visualizer_container

        self.visualizer_frame = Gtk.Frame()
        self.visualizer_frame.set_margin_start(10)
        self.visualizer_frame.set_margin_end(10)
        self.visualizer_frame.set_margin_top(4)
        self.visualizer_frame.set_margin_bottom(0)

        self.visualizer_frame.set_size_request(-1, 100)

        # Connect seek handler before adding the visualizer to the frame
        self.visualizer.connect_seek_handler(self.on_visualizer_seek)

        self.visualizer.connect_marker_drag_handler(self._on_marker_drag_state_changed)

        self.visualizer.zoom_changed_callback = weak_callback(
            self._on_visualizer_zoom_changed
        )
        # Panning while paused moves the window too; the seek bar follows it.
        self.visualizer.viewport_changed_callback = self._sync_seekbar_viewport

        self.visualizer.marker_updated_callback = self._on_markers_updated

        self.visualizer.set_content_height(100)

        self.visualizer.set_vexpand(True)  # Allow expansion
        self.visualizer.set_focusable(True)

        self.visualizer_frame.set_child(self.visualizer)

        # Layout order: waveform (top) → seekbar (middle) → button bar (bottom)
        visualizer_container.append(self.visualizer_frame)

        # The time read at both ends of the seek bar.
        self.elapsed_label = Gtk.Label(label="0:00", css_classes=["numeric", "caption"])
        self.total_label = Gtk.Label(label="0:00", css_classes=["numeric", "caption"])
        self.seekbar = SeekBar(self.elapsed_label, self.total_label)
        self.seekbar.set_hexpand(True)
        self.visualizer.duration_changed_callback = self.seekbar.set_duration
        self.seekbar.connect_seek_handler(self.on_visualizer_seek)
        self.visualizer.hover_time_callback = self.seekbar.set_waveform_hover_time
        # The seek bar's track spans the waveform inside its frame (10 px of
        # margin and 1 px of border), its thumb's 6 px reaching past either
        # end, so the playhead and the thumb meet; the times sit under its ends.
        self.seekbar.set_margin_start(5)
        self.seekbar.set_margin_end(5)
        visualizer_container.append(self.seekbar)
        visualizer_container.append(
            Gtk.CenterBox(
                margin_start=11,
                margin_end=11,
                start_widget=self.elapsed_label,
                end_widget=self.total_label,
            )
        )

        visualizer_container.append(self.controls_layout)

        self.vertical_paned.set_end_child(visualizer_container)
        self._fit_pending = False
        self.vertical_paned.connect(
            "notify::max-position", weak_callback(self._on_paned_allocated)
        )

    def _bar_control(self, parent, name, icon, title, adjustment, extras, magnets=()):
        """A button and readout whose slider opens on hover, as in the players.

        The card's heading names what the slider changes. A click opens it
        for the keyboard; the wheel over the control moves the slider.
        """
        owner = weakref.proxy(self)
        button = Gtk.Button(
            icon_name=icon, valign=Gtk.Align.CENTER, css_classes=["flat", "circular"]
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        value = Gtk.Label(css_classes=["numeric"])
        box = Gtk.Box(spacing=2, css_classes=["bar-anchor"])
        box.append(button)
        box.append(value)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.VERTICAL,
            adjustment=adjustment,
            inverted=True,
            draw_value=False,
            height_request=180,
            halign=Gtk.Align.CENTER,
        )
        scale.update_property([Gtk.AccessibleProperty.LABEL], [title])
        for position in magnets:
            scale.add_mark(position, Gtk.PositionType.RIGHT, None)

        def snap(_scale, scroll, value):
            # Only a drag or a click is pulled in; arrows and the wheel step past.
            if scroll != Gtk.ScrollType.JUMP or not magnets:
                return False
            nearest = min(magnets, key=lambda position: abs(position - value))
            if abs(nearest - value) > MAGNET_REACH:
                return False
            adjustment.set_value(nearest)
            return True

        scale.connect("change-value", snap)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=10,
            margin_bottom=10,
            margin_start=10,
            margin_end=10,
        )
        content.append(Gtk.Label(label=title, css_classes=["heading"]))
        content.append(scale)
        for widget in extras:
            # Tiles fill the card as in the sidebar's; centered, their
            # wrapping names would be measured for a height they do not fit.
            if not isinstance(widget, ChoiceTiles):
                widget.set_halign(Gtk.Align.CENTER)
            content.append(widget)
        self.bar_cards.attach(
            name, box, content, title, ["bar-slider"], focus=scale, point_at=button
        )
        button.connect("clicked", lambda _b: owner.bar_cards.open_for_keyboard(name))
        wheel = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )

        def scrolled(_controller, _dx, dy):
            if not dy or not scale.get_sensitive():
                return False
            adjustment.set_value(adjustment.get_value() - 2 * dy)
            return True

        wheel.connect("scroll", scrolled)
        box.add_controller(wheel)
        parent.append(box)
        return button, value, scale

    def _on_zoom_slider_changed(self, adjustment):
        if self._zoom_from_waveform:
            return
        zoom = self.visualizer.max_zoom ** (adjustment.get_value() / 100)
        self.visualizer.set_zoom_level(zoom, use_mouse_position=False)
        self._on_visualizer_zoom_changed(self.visualizer.zoom_level)

    def _on_visualizer_zoom_changed(self, zoom_level):
        # 1×, 1.5×, 32×, 1000×: one decimal only where it tells levels apart.
        text = f"{zoom_level:.1f}".rstrip("0").rstrip(".")
        self.zoom_value.set_label(f"{text if zoom_level < 10 else round(zoom_level)}×")
        self.zoom_reset_button.set_sensitive(zoom_level > 1.0)
        self._zoom_from_waveform = True
        self.zoom_adjustment.set_value(
            100 * math.log(zoom_level) / math.log(self.visualizer.max_zoom)
        )
        self._zoom_from_waveform = False
        self._sync_seekbar_viewport()

    def _sync_seekbar_viewport(self):
        """Show the waveform's visible window on the seek bar."""
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )

    def _set_player_visible(self, visible):
        """Without files the queue has the whole window."""
        if visible == self.visualizer_container.get_visible():
            return
        self._remember_waveform_height()
        self.visualizer_container.set_visible(visible)
        if visible:
            self._fit_bottom_panel()

    def _on_paned_allocated(self, *_args):
        if self._fit_pending:
            self._fit_pending = False
            self._sources.idle(self._fit_bottom_panel)

    def _remember_waveform_height(self):
        """Keep the height the person dragged the waveform to."""
        height = self.visualizer_frame.get_height()
        if self.visualizer_container.get_visible() and height >= 100:
            self.visualizer_height = height
            self.app.config.set("visualizer_height", str(height))

    def _fit_bottom_panel(self):
        """Give the bottom panel its controls, plus the waveform while cutting."""
        paned = self.vertical_paned
        if not self.visualizer_container.get_visible():
            return
        if paned.get_height() <= 0:
            # Not allocated yet: the paned's first allocation retries.
            self._fit_pending = True
            return
        # The measured minimum holds the waveform at its 100 px floor.
        panel = self.visualizer_container.measure(
            Gtk.Orientation.VERTICAL, paned.get_width()
        )[0]
        extra = self.visualizer_height - 100
        handle = (
            paned.get_height()
            - self.split_view.get_height()
            - self.visualizer_container.get_height()
        )
        paned.set_position(max(200, paned.get_height() - handle - panel - extra))

    def playback_menu_section(self):
        """The main menu's listening options; converted files keep none of them."""
        self.auto_advance_action = Gio.SimpleAction.new_stateful(
            "auto-advance",
            None,
            GLib.Variant.new_boolean(
                self.app.config.get("auto_advance_enabled", True) is True
            ),
        )
        self.auto_advance_action.connect(
            "change-state", weak_callback(self._on_auto_advance_changed)
        )
        self.add_action(self.auto_advance_action)
        self.audio_output_action = Gio.SimpleAction.new_stateful(
            "audio-output",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self.player.audio_device),
        )
        self.audio_output_action.connect(
            "change-state", weak_callback(self._on_audio_output_changed)
        )
        self.add_action(self.audio_output_action)
        self.audio_output_menu = Gio.Menu()
        self._refresh_audio_outputs()
        self.player.devices_callback = weak_callback(self._refresh_audio_outputs)
        section = Gio.Menu()
        section.append(_("Play the next file automatically"), "win.auto-advance")
        section.append_submenu(_("Audio output"), self.audio_output_menu)
        return section

    def _on_auto_advance_changed(self, action, value):
        action.set_state(value)
        self.app.config.set("auto_advance_enabled", value.get_boolean())

    def _refresh_audio_outputs(self):
        devices = [
            device for device in self.player.audio_devices if device["name"] != "auto"
        ]
        # Desktop audio servers expose the useful outputs without duplicate ALSA routes.
        for backend in ("pipewire/", "pulse/"):
            managed = [
                device for device in devices if device["name"].startswith(backend)
            ]
            if managed:
                devices = [
                    device
                    for device in devices
                    if device in managed or device["name"] == self.player.audio_device
                ]
                break
        self.audio_output_menu.remove_all()
        for name, label in [("auto", _("System default"))] + [
            (device["name"], device["description"]) for device in devices
        ]:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "win.audio-output", GLib.Variant.new_string(name)
            )
            self.audio_output_menu.append_item(item)
        self.audio_output_action.set_state(
            GLib.Variant.new_string(self.player.audio_device)
        )

    def _on_audio_output_changed(self, action, value):
        if self.player.set_audio_device(value.get_string()):
            action.set_state(value)
        else:
            self._refresh_audio_outputs()
