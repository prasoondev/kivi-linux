"""The kivi look, lifted from heykivi.ai's stylesheet: dusk greens, cream ink, Fraunces + Switzer.

`OVERLAY_CSS` styles the desktop pill window, `SETTINGS_CSS` the Adw manager app.
`TOKENS` / `rgba()` expose the same palette to cairo drawing code.
"""

TOKENS = {
    "stage": "#0b0e0a",
    "dusk-0": "#0b0e0a",
    "dusk-1": "#121710",
    "dusk-2": "#1a2016",
    "ink": "#f4f1e6",
    "ink-2": "#b9b6a8",
    "ink-3": "#7d7b6f",
    "hairline": "rgba(255,255,255,0.08)",
    "hairline-strong": "rgba(255,255,255,0.12)",
    "paper": "#f6f4ec",
    "green-glow": "#a8e063",
    "green-deep": "#256b2f",
    "green-tint": "rgba(168,224,99,0.18)",
    "amber": "#c77e28",
    "error": "#ff6b5b",
    "keycap-ink": "#efeade",
}

RADII = {"r1": 4, "r2": 8, "r3": 12, "r4": 20, "pill": 999}

FONT_DISPLAY = "Fraunces"
FONT_BODY = "Switzer Variable"
FONT_MONO = "Geist Mono"

# rest scales of the five listening bars (the site's --bar-rest values)
BAR_REST = (0.42, 0.78, 0.56, 0.90, 0.64)

# cubic-bezier(0.16, 1, 0.3, 1), the site's --ease-enter
EASE_ENTER = (0.16, 1.0, 0.3, 1.0)
EASE_ENTER_MS = 250


def rgba(color: str, alpha: float | None = None) -> tuple[float, float, float, float]:
    """Token name or #hex / rgba() string -> (r, g, b, a) floats for cairo."""
    c = TOKENS.get(color, color).strip()
    if c.startswith("#"):
        h = c[1:]
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        a = 1.0
    else:
        parts = c[c.index("(") + 1:c.rindex(")")].split(",")
        r, g, b = (float(p) / 255 for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
    return r, g, b, a if alpha is None else alpha


_FONTS = f"""
@define-color kivi_stage #0b0e0a;
@define-color kivi_dusk_1 #121710;
@define-color kivi_dusk_2 #1a2016;
@define-color kivi_ink #f4f1e6;
@define-color kivi_ink_2 #b9b6a8;
@define-color kivi_ink_3 #7d7b6f;
@define-color kivi_green #a8e063;
@define-color kivi_green_deep #256b2f;
@define-color kivi_amber #c77e28;
@define-color kivi_error #ff6b5b;

.kivi-display {{ font-family: "{FONT_DISPLAY}", Georgia, serif; font-weight: 400; }}
.kivi-body {{ font-family: "{FONT_BODY}", "Switzer", system-ui, sans-serif; }}
.kivi-mono {{ font-family: "{FONT_MONO}", "JetBrains Mono", "Noto Sans Mono", monospace; }}
"""

OVERLAY_CSS = _FONTS + f"""
window.kivi-overlay,
window.kivi-overlay > contents,
window.kivi-overlay .kivi-root {{
  background: transparent;
  box-shadow: none;
  border: none;
  outline: none;
}}
window.kivi-overlay {{
  font-family: "{FONT_BODY}", system-ui, sans-serif;
  color: @kivi_ink;
}}

/* reply / menu card that floats above the pill */
.kivi-card {{
  background-color: @kivi_dusk_1;
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 20px;
  padding: 16px 18px 12px 18px;
  box-shadow: 0 0 0 1px rgba(6, 10, 5, 0.25),
              0 4px 10px rgba(6, 10, 5, 0.30),
              0 14px 24px -10px rgba(6, 10, 5, 0.50);
  margin: 0 18px;
}}
.kivi-card .kivi-asked-label {{
  color: alpha(@kivi_ink, 0.45);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
}}
.kivi-card .kivi-asked {{
  color: @kivi_ink_3;
  font-size: 12px;
}}
.kivi-card .kivi-answer {{
  font-family: "{FONT_DISPLAY}", Georgia, serif;
  font-size: 15px;
  font-weight: 400;
  color: @kivi_ink;
}}
.kivi-card .kivi-card-foot {{
  border-top: 1px solid rgba(255, 255, 255, 0.07);
  padding-top: 8px;
  margin-top: 4px;
}}
.kivi-card .kivi-brand {{
  font-family: "{FONT_DISPLAY}", Georgia, serif;
  font-size: 13px;
  color: @kivi_ink_3;
}}
.kivi-card button.kivi-chip {{
  min-height: 0;
  min-width: 0;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid rgba(168, 224, 99, 0.26);
  background: rgba(168, 224, 99, 0.12);
  color: #d6efc1;
  font-size: 11px;
  font-weight: 500;
  box-shadow: none;
}}
.kivi-card button.kivi-chip:hover {{ background: rgba(168, 224, 99, 0.22); }}
.kivi-card button.kivi-ghost {{
  min-height: 0;
  min-width: 0;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: transparent;
  color: @kivi_ink_3;
  font-size: 11px;
  box-shadow: none;
}}
.kivi-card button.kivi-ghost:hover {{ color: @kivi_ink; background: rgba(255, 255, 255, 0.05); }}

/* right-click menu, drawn as a small card */
.kivi-menu {{ padding: 6px; border-radius: 14px; }}
.kivi-menu button {{
  min-height: 0;
  padding: 7px 12px;
  border-radius: 8px;
  background: transparent;
  color: @kivi_ink;
  font-size: 13px;
  box-shadow: none;
  border: none;
}}
.kivi-menu button label {{ font-weight: 450; }}
.kivi-menu button:hover {{ background: rgba(168, 224, 99, 0.12); color: #e6f5d4; }}
.kivi-menu button.kivi-danger:hover {{ background: rgba(255, 107, 91, 0.12); color: #ffb3aa; }}
.kivi-menu separator {{ background: rgba(255, 255, 255, 0.07); margin: 4px 8px; min-height: 1px; }}
.kivi-menu .kivi-menu-head {{
  font-family: "{FONT_DISPLAY}", Georgia, serif;
  font-size: 15px;
  color: @kivi_ink;
  padding: 4px 12px 6px 12px;
}}
"""

SETTINGS_CSS = _FONTS + f"""
/* Adwaita, re-inked: dusk ground, cream text, green accent */
:root {{
  --window-bg-color: #0b0e0a;
  --window-fg-color: #f4f1e6;
  --view-bg-color: #0f130d;
  --view-fg-color: #f4f1e6;
  --headerbar-bg-color: #0b0e0a;
  --headerbar-fg-color: #f4f1e6;
  --headerbar-border-color: rgba(255, 255, 255, 0.08);
  --headerbar-shade-color: rgba(0, 0, 0, 0.0);
  --sidebar-bg-color: #0e120c;
  --sidebar-fg-color: #f4f1e6;
  --sidebar-backdrop-color: #0e120c;
  --sidebar-shade-color: rgba(0, 0, 0, 0.25);
  --sidebar-border-color: rgba(255, 255, 255, 0.08);
  --secondary-sidebar-bg-color: #0e120c;
  --card-bg-color: #121710;
  --card-fg-color: #f4f1e6;
  --card-shade-color: rgba(0, 0, 0, 0.3);
  --dialog-bg-color: #121710;
  --dialog-fg-color: #f4f1e6;
  --popover-bg-color: #1a2016;
  --popover-fg-color: #f4f1e6;
  --thumbnail-bg-color: #1a2016;
  --accent-bg-color: #a8e063;
  --accent-fg-color: #0b0e0a;
  --accent-color: #a8e063;
  --destructive-bg-color: #ff6b5b;
  --destructive-fg-color: #0b0e0a;
  --destructive-color: #ff6b5b;
  --success-color: #a8e063;
  --warning-color: #c77e28;
  --error-color: #ff6b5b;
  --border-color: rgba(255, 255, 255, 0.10);
  --window-radius: 14px;
}}

window, dialog {{ font-family: "{FONT_BODY}", "Switzer", system-ui, sans-serif; }}
window.kivi-settings {{ background-color: #0b0e0a; color: #f4f1e6; }}

/* headings & hero */
.kivi-wordmark {{ font-family: "{FONT_DISPLAY}", Georgia, serif; font-size: 22px; font-weight: 500; color: #f4f1e6; }}
.kivi-hero {{ font-family: "{FONT_DISPLAY}", Georgia, serif; font-size: 44px; font-weight: 400; letter-spacing: -0.02em; color: #f4f1e6; }}
.kivi-hero-accent {{ color: #a8e063; }}
.kivi-title {{ font-family: "{FONT_DISPLAY}", Georgia, serif; font-size: 26px; font-weight: 400; letter-spacing: -0.01em; color: #f4f1e6; }}
.kivi-subtitle {{ color: #b9b6a8; font-size: 14px; }}
.kivi-eyebrow {{ color: alpha(#f4f1e6, 0.5); font-size: 11px; font-weight: 600; letter-spacing: 0.08em; }}
.kivi-dim {{ color: #7d7b6f; }}
.kivi-ink-2 {{ color: #b9b6a8; }}
.kivi-green {{ color: #a8e063; }}
.kivi-amber {{ color: #c77e28; }}
.kivi-error {{ color: #ff6b5b; }}
.kivi-ok {{ color: #a8e063; font-weight: 600; }}
.kivi-bad {{ color: #ff6b5b; font-weight: 600; }}

/* sidebar */
.navigation-sidebar row, .kivi-sidebar row {{ border-radius: 8px; margin: 1px 6px; padding: 8px 10px; }}
.navigation-sidebar row:selected, .kivi-sidebar row:selected {{ background: rgba(168, 224, 99, 0.14); color: #e6f5d4; }}
.navigation-sidebar row:hover:not(:selected) {{ background: rgba(255, 255, 255, 0.04); }}

/* cards & lists */
.card, .kivi-card, list.boxed-list {{
  background-color: #121710;
  border-radius: 12px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08), 0 4px 24px rgba(10, 12, 8, 0.16);
}}
.kivi-card {{ padding: 18px 20px; }}
list.boxed-list > row {{ border-color: rgba(255, 255, 255, 0.06); }}
row.activatable:hover {{ background-color: rgba(255, 255, 255, 0.03); }}

/* buttons */
button.suggested-action, button.kivi-primary {{
  background: #a8e063;
  color: #0b0e0a;
  border-radius: 8px;
  font-weight: 600;
}}
button.suggested-action:hover, button.kivi-primary:hover {{ background: #b8ea78; }}
button.kivi-pill {{ border-radius: 999px; padding: 4px 14px; }}
switch:checked {{ background-color: #a8e063; }}
switch:checked > slider {{ background-color: #0b0e0a; }}
scale highlight {{ background-color: #a8e063; }}
entry, text, textview, .kivi-input {{ caret-color: #a8e063; }}
textview.kivi-textarea, textview.kivi-textarea text {{
  background-color: #0f130d;
  color: #f4f1e6;
  font-size: 15px;
}}
textview.kivi-textarea {{ border-radius: 12px; padding: 12px; border: 1px solid rgba(255, 255, 255, 0.10); }}
selection {{ background-color: rgba(168, 224, 99, 0.30); color: #f4f1e6; }}

/* keycaps: skirt + lighter face + cream mono legend, like the site's .keycap */
.keycap {{
  font-family: "{FONT_MONO}", "JetBrains Mono", "Noto Sans Mono", monospace;
  font-size: 12px;
  color: #efeade;
  min-width: 22px;
  padding: 5px 10px 6px 10px;
  border-radius: 7px;
  background-image: linear-gradient(176deg, #5a6353 0%, #414937 46%, #333a2e 100%);
  box-shadow: inset 0 1px 0 rgba(246, 243, 234, 0.28),
              inset 0 -3px 0 #171a15,
              0 0 0 1px rgba(12, 14, 12, 0.6),
              0 3px 4px rgba(6, 10, 5, 0.45);
}}
.keycap.held, .keycap.kivi-held {{
  box-shadow: inset 0 1px 0 rgba(246, 243, 234, 0.28),
              inset 0 -2px 0 #171a15,
              0 0 0 2px rgba(168, 224, 99, 0.62),
              0 0 0 7px rgba(168, 224, 99, 0.16);
}}
.keycap-plus {{ color: #7d7b6f; font-size: 12px; margin: 0 2px; }}

/* chips: "you said" / "kivi remembered" */
.kivi-chip {{
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid rgba(168, 224, 99, 0.26);
  background: rgba(168, 224, 99, 0.12);
  color: #d6efc1;
  font-size: 11px;
  font-weight: 500;
}}
button.kivi-chip {{ min-height: 0; box-shadow: none; }}
button.kivi-chip:hover {{ background: rgba(168, 224, 99, 0.22); }}
.kivi-tag {{
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
  color: #b9b6a8;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
}}
.kivi-tag.dictate {{ background: rgba(168, 224, 99, 0.12); color: #d6efc1; }}
.kivi-tag.command {{ background: rgba(199, 126, 40, 0.16); color: #f0c592; }}
.kivi-tag.agent {{ background: rgba(37, 107, 47, 0.35); color: #cfeeb0; }}
.kivi-said-label {{ color: alpha(#f4f1e6, 0.45); font-size: 10px; font-weight: 600; letter-spacing: 0.08em; }}
.kivi-said {{ color: #b9b6a8; font-size: 13px; }}
.kivi-result {{ color: #f4f1e6; font-size: 14px; }}
.kivi-code {{
  font-family: "{FONT_MONO}", "JetBrains Mono", "Noto Sans Mono", monospace;
  font-size: 12px;
  color: #d6efc1;
  background: rgba(168, 224, 99, 0.08);
  border: 1px solid rgba(168, 224, 99, 0.18);
  border-radius: 8px;
  padding: 8px 10px;
}}
"""


def apply(css: str) -> None:
    """Install `css` on the default display at USER priority (beats the theme and Adwaita)."""
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
