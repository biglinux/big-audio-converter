"""Pango shaping and font fallback for the existing Cairo canvas labels."""
from typing import NamedTuple

import cairo
import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo


class TextExtents(NamedTuple):
    x_bearing: float
    y_bearing: float
    width: float
    height: float
    x_advance: float
    y_advance: float


def _layout(cr, text):
    layout = PangoCairo.create_layout(cr)
    layout.set_text(str(text), -1)
    layout.set_auto_dir(True)
    font = cr.get_font_face()
    description = Pango.FontDescription()
    description.set_family(font.get_family() if isinstance(font, cairo.ToyFontFace) else "Sans")
    if isinstance(font, cairo.ToyFontFace) and font.get_weight() == cairo.FONT_WEIGHT_BOLD:
        description.set_weight(Pango.Weight.BOLD)
    settings = Gtk.Settings.get_default()
    configured = Pango.FontDescription.from_string(settings.get_property("gtk-font-name")) if settings else None
    # GTK scales device pixels itself; this factor honors the user's text size.
    factor = max(1.0, configured.get_size() / (11 * Pango.SCALE)) if configured and configured.get_size() else 1.0
    description.set_absolute_size(max(1, abs(cr.get_font_matrix().yy)) * factor * Pango.SCALE)
    layout.set_font_description(description)
    return layout


def text_extents(cr, text):
    layout = _layout(cr, text)
    _, logical = layout.get_extents()
    scale = Pango.SCALE
    return TextExtents(logical.x / scale, (logical.y - layout.get_baseline()) / scale,
                       logical.width / scale, logical.height / scale, logical.width / scale, 0.0)


def show_text(cr, text):
    layout = _layout(cr, text)
    x, baseline = cr.get_current_point()
    cr.move_to(x, baseline - layout.get_baseline() / Pango.SCALE)
    PangoCairo.show_layout(cr, layout)
    cr.move_to(x + layout.get_size()[0] / Pango.SCALE, baseline)
