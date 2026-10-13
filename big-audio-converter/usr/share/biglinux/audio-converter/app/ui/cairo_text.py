"""Cairo canvas helpers: Pango-shaped labels with font fallback, rounded paths."""

import math
from typing import NamedTuple

import cairo
import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo

# Fixed roles on the always-dark playback strip. The waveform reads its blue
# from CSS; the parts kept are green and the parts cut out red, apart from it.
INK = (1.0, 1.0, 1.0)  # neutral marks, always drawn with an alpha
CUT = (0.9, 0.25, 0.25)  # red: parts cut out
KEEP = (0.25, 0.8, 0.4)  # green: parts kept


class TextExtents(NamedTuple):
    width: float
    height: float


def _layout(cr, text):
    layout = PangoCairo.create_layout(cr)
    layout.set_text(str(text), -1)
    layout.set_auto_dir(True)
    font = cr.get_font_face()
    description = Pango.FontDescription()
    description.set_family(
        font.get_family() if isinstance(font, cairo.ToyFontFace) else "Sans"
    )
    if (
        isinstance(font, cairo.ToyFontFace)
        and font.get_weight() == cairo.FONT_WEIGHT_BOLD
    ):
        description.set_weight(Pango.Weight.BOLD)
    settings = Gtk.Settings.get_default()
    configured = (
        Pango.FontDescription.from_string(settings.get_property("gtk-font-name"))
        if settings
        else None
    )
    # GTK scales device pixels itself; this factor honors the user's text size.
    factor = (
        max(1.0, configured.get_size() / (11 * Pango.SCALE))
        if configured and configured.get_size()
        else 1.0
    )
    description.set_absolute_size(
        max(1, abs(cr.get_font_matrix().yy)) * factor * Pango.SCALE
    )
    layout.set_font_description(description)
    return layout


def text_extents(cr, text):
    layout = _layout(cr, text)
    _, logical = layout.get_extents()
    return TextExtents(logical.width / Pango.SCALE, logical.height / Pango.SCALE)


def show_text(cr, text):
    layout = _layout(cr, text)
    x, baseline = cr.get_current_point()
    cr.move_to(x, baseline - layout.get_baseline() / Pango.SCALE)
    PangoCairo.show_layout(cr, layout)
    cr.move_to(x + layout.get_size()[0] / Pango.SCALE, baseline)


def rounded_rect(cr, x, y, width, height, radius):
    """Add a rounded rectangle to the current path."""
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()
