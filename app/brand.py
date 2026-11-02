"""
VigiEye brand assets: the logo mark (a benzene-ring hexagon with a signal trace running
through it: chemistry + continuous monitoring) and the favicon/touch-icon links.

The image files live in app/static/brand/ and are also served at the conventional root
paths (/favicon.ico, /favicon.svg, /apple-touch-icon.png, /site.webmanifest) by main.py.
"""
from __future__ import annotations

from itertools import count

_ids = count(1)

# <link> tags for every page <head>.
HEAD_ICONS = (
    '<link rel="icon" href="/favicon.ico" sizes="any">'
    '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
    '<link rel="manifest" href="/site.webmanifest">'
    '<meta name="theme-color" content="#0b1324">'
)

# Hosted PNG for e-mail clients, which do not render inline SVG.
MARK_PNG_PATH = "/static/brand/mark-128.png"


def mark_svg(size: int = 26, light: bool = False) -> str:
    """Inline SVG of the logo mark. Each call gets its own gradient id so several marks
    can sit on one page. `light` uses brighter stops for dark backgrounds."""
    gid = f"vgm{next(_ids)}"
    a, b = ("#38bdf8", "#2dd4bf") if light else ("#0ea5e9", "#0d9488")
    return (
        f'<svg class="vg-mark" width="{size}" height="{size}" viewBox="0 0 32 32" fill="none" '
        f'aria-hidden="true" focusable="false" style="flex:none;vertical-align:middle">'
        f'<defs><linearGradient id="{gid}" x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">'
        f'<stop stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient></defs>'
        f'<path d="M16 3.5 26.8 9.75v12.5L16 28.5 5.2 22.25V9.75Z" stroke="url(#{gid})" '
        f'stroke-width="2.4" stroke-linejoin="round"/>'
        f'<path d="M5.2 16h5.3l2.3-5.2 3.6 10.6 2.5-7.2 1.3 1.8h6.6" stroke="url(#{gid})" '
        f'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
