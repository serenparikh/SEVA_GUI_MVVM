"""Shared helper namespace for view-layer utility functions.

A real design system for the ttk-only redesign: color tokens, a one-shot
theming helper that configures every ttk widget class used across the app
(not just a couple of buttons), named-style helpers for the well grid and
channel activity matrix (so those are plain ttk.Button/ttk.Label instead of
raw tk widgets with hand-set bg colors), and a small Pillow wrapper for the
logo image.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Optional

__all__ = [
    "ACCENT",
    "ACCENT_HOVER",
    "ACCENT_SOFT",
    "CONFIGURED",
    "PAGE_BG",
    "CARD_BG",
    "BORDER",
    "TEXT",
    "TEXT_MUTED",
    "STATUS_COLORS",
    "apply_theme",
    "load_logo_image",
    "well_style_name",
    "status_style_name",
]

# Accent derived from the SEVA logo's own pixel color (#F2802C), deepened
# just enough to clear WCAG AA contrast (4.5:1) as white button text.
ACCENT = "#C0580C"
ACCENT_HOVER = "#A84B0A"
ACCENT_SOFT = "#FCEEE3"
CONFIGURED = "#D9F0DB"
CONFIGURED_BORDER = "#8FCB95"

PAGE_BG = "#FFFFFF"
CARD_BG = "#FFFFFF"
BORDER = "#DADDE3"
BORDER_SOFT = "#E8EAED"
TEXT = "#1C2130"
TEXT_MUTED = "#6B7280"

STATUS_COLORS = {
    "Idle": "#FFFFFF",
    "Queued": "#FDE9CF",
    "Running": CONFIGURED,
    "Done": "#D6E6FB",
    "Error": "#FBD9D9",
}

BASE_FONT = ("Segoe UI", 10)
BOLD_FONT = ("Segoe UI", 10, "bold")
SMALL_FONT = ("Segoe UI", 9)
SMALL_BOLD_FONT = ("Segoe UI", 9, "bold")


def apply_theme(root: tk.Misc) -> None:
    """Configure shared ttk styling once for the whole application.

    Covers every ttk widget class used across the app (buttons, frames,
    labelframes, labels, entries, comboboxes, checkbuttons, progressbars,
    treeviews, scrollbars) so the visual language is consistent everywhere,
    not just on a couple of accent buttons.

    Args:
        root: The Tk root (or any widget) used to construct the ttk.Style.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # ---- Base ----
    style.configure(".", font=BASE_FONT, background=PAGE_BG, foreground=TEXT)
    style.configure("TFrame", background=PAGE_BG)
    style.configure("TLabel", background=PAGE_BG, foreground=TEXT)
    style.configure("TCheckbutton", background=PAGE_BG, foreground=TEXT)

    # ---- Card surfaces (Plate panel, status rail sections, experiment
    # mode groups) ----
    style.configure(
        "Card.TFrame", background=CARD_BG, relief="solid", borderwidth=1,
    )
    # Borderless variant for nested containers that just need to match a
    # card's background (e.g. a legend row inside a Rail.TLabelframe).
    style.configure("CardPlain.TFrame", background=CARD_BG)
    style.configure(
        "Card.TLabelframe",
        background=CARD_BG,
        bordercolor=BORDER,
        relief="solid",
        borderwidth=1,
        padding=10,
    )
    style.configure(
        "Card.TLabelframe.Label", background=CARD_BG, foreground=TEXT, font=BOLD_FONT,
    )
    style.configure("Card.TLabel", background=CARD_BG, foreground=TEXT)
    style.configure("CardMuted.TLabel", background=CARD_BG, foreground=TEXT_MUTED, font=SMALL_FONT)
    style.configure("Card.TCheckbutton", background=CARD_BG, foreground=TEXT, font=BOLD_FONT)

    # Status rail cards are smaller/denser than the Plate card.
    style.configure(
        "Rail.TLabelframe",
        background=CARD_BG,
        bordercolor=BORDER,
        relief="solid",
        borderwidth=1,
        padding=8,
    )
    style.configure("Rail.TLabelframe.Label", background=CARD_BG, foreground=TEXT, font=SMALL_BOLD_FONT)
    style.configure("Rail.TLabel", background=CARD_BG, foreground=TEXT, font=SMALL_FONT)
    style.configure("RailMuted.TLabel", background=CARD_BG, foreground=TEXT_MUTED, font=("Segoe UI", 8))

    # ---- Buttons ----
    style.configure(
        "TButton",
        padding=(10, 6),
        background=CARD_BG,
        foreground=TEXT,
        bordercolor=BORDER,
        relief="solid",
        borderwidth=1,
        focusthickness=0,
    )
    style.map(
        "TButton",
        background=[("active", "#F2F3F6"), ("pressed", "#E8EAED")],
        bordercolor=[("focus", ACCENT)],
    )
    style.configure(
        "Accent.TButton",
        padding=(14, 7),
        background=ACCENT,
        foreground="white",
        bordercolor=ACCENT,
        relief="flat",
        borderwidth=0,
        font=BOLD_FONT,
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER)],
        foreground=[("disabled", "#ffffff")],
    )
    style.configure(
        "Danger.TButton",
        padding=(10, 6),
        background="#FFF5F5",
        foreground="#B23B3B",
        bordercolor="#EBC4C4",
        relief="solid",
        borderwidth=1,
    )
    style.map("Danger.TButton", background=[("active", "#FDE7E7")])
    style.configure("Link.TButton", padding=(4, 2), background=CARD_BG, foreground=ACCENT, relief="flat", borderwidth=0)
    style.map("Link.TButton", foreground=[("active", ACCENT_HOVER)])

    # ---- Inputs ----
    style.configure(
        "TEntry", fieldbackground="#FFFFFF", bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=5,
    )
    style.map("TEntry", bordercolor=[("focus", ACCENT)])
    style.configure(
        "TCombobox", fieldbackground="#FFFFFF", bordercolor=BORDER, arrowcolor=TEXT_MUTED, padding=4,
    )

    # ---- Progressbar ----
    style.configure(
        "TProgressbar", background=ACCENT, troughcolor=BORDER_SOFT, bordercolor=BORDER_SOFT, thickness=6,
    )

    # ---- Treeview (Run Overview / Runs dialogs) ----
    style.configure(
        "Treeview",
        background="#FFFFFF",
        fieldbackground="#FFFFFF",
        foreground=TEXT,
        bordercolor=BORDER,
        rowheight=26,
        font=BASE_FONT,
    )
    style.configure("Treeview.Heading", background=PAGE_BG, foreground=TEXT_MUTED, font=SMALL_BOLD_FONT, relief="flat")
    style.map(
        "Treeview",
        background=[("selected", ACCENT_SOFT)],
        foreground=[("selected", TEXT)],
    )

    # ---- Scrollbars ----
    style.configure("TScrollbar", background=BORDER_SOFT, troughcolor=PAGE_BG, bordercolor=PAGE_BG, arrowsize=12)

    _configure_well_and_status_styles(style)


def well_style_name(*, selected: bool, configured: bool) -> str:
    """Return the ttk.Button style name for a well cell's current state.

    Args:
        selected: Whether the well is currently selected (accent border).
        configured: Whether the well has saved params (green fill).
    """
    suffix = ("Selected" if selected else "") + ("Configured" if configured else "")
    return f"Well{suffix}.TButton" if suffix else "Well.TButton"


def status_style_name(status: str) -> str:
    """Return the ttk.Label style name for a channel-activity status."""
    return f"Status{status}.TLabel" if status in STATUS_COLORS else "Status.TLabel"


def _configure_well_and_status_styles(style: ttk.Style) -> None:
    """Register the small fixed set of named styles the well grid and
    channel activity matrix need, instead of per-widget raw bg colors.
    """
    # Well grid: 4 state combinations (configured x selected). Border width
    # and font are identical across all four -- only color changes -- so a
    # well's rendered size never changes on click (a changing borderwidth
    # or bold/non-bold font swap shifts neighboring buttons in the grid).
    style.configure(
        "Well.TButton", background="#FFFFFF", bordercolor=BORDER, relief="solid",
        borderwidth=2, padding=(2, 3), font=SMALL_FONT,
    )
    style.configure(
        "WellConfigured.TButton", background=CONFIGURED, bordercolor=CONFIGURED_BORDER,
        relief="solid", borderwidth=2, padding=(2, 3), font=SMALL_FONT,
    )
    style.configure(
        "WellSelected.TButton", background="#FFFFFF", bordercolor=ACCENT,
        relief="solid", borderwidth=2, padding=(2, 3), font=SMALL_FONT,
    )
    style.configure(
        "WellSelectedConfigured.TButton", background=CONFIGURED, bordercolor=ACCENT,
        relief="solid", borderwidth=2, padding=(2, 3), font=SMALL_FONT,
    )
    for name in ("Well.TButton", "WellConfigured.TButton", "WellSelected.TButton", "WellSelectedConfigured.TButton"):
        style.map(name, background=[("active", "#F2F3F6")])

    # Channel activity matrix: one style per status color.
    for status, color in STATUS_COLORS.items():
        style.configure(
            f"Status{status}.TLabel", background=color, bordercolor=BORDER,
            relief="solid", borderwidth=1, anchor="center", font=("Segoe UI", 8),
        )


def load_logo_image(path: Optional[Path] = None) -> Optional["tk.PhotoImage"]:
    """Load the SEVA logo as a Tk-displayable image, if available.

    Args:
        path: Optional explicit path; defaults to seva/app/assets/seva_logo.png.

    Returns:
        A PhotoImage-compatible object, or None if Pillow or the file is unavailable.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / "assets" / "seva_logo.png"
    if not path.exists():
        return None
    try:
        from PIL import Image, ImageTk
    except ImportError:
        return None
    image = Image.open(path)
    image.thumbnail((160, 48), Image.LANCZOS)
    return ImageTk.PhotoImage(image)
