"""Read-only activity matrix view for box/well status rendering.

This view renders an A/B/C/... matrix of small status cells and exposes
setter-style methods used by progress viewmodels. It performs no I/O.
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from seva.app.views import view_utils

WellId = str  # e.g., "A1"
BoxId = str   # e.g., "A"


class ChannelActivityView(ttk.Frame):
    """Compact, read-only activity matrix sized to fit the status rail."""

    def __init__(self, parent: tk.Widget, *, boxes: Sequence[BoxId] = ("A","B","C","D")) -> None:
        """Build matrix tab widgets.

        Args:
            parent: Notebook tab container.
            boxes: Ordered list of box identifiers to render.
        """
        super().__init__(parent, style="CardPlain.TFrame")
        self._boxes: List[BoxId] = list(boxes)
        self._wells_per_box: int = 10
        self._cells: Dict[WellId, tk.Label] = {}

        # Header: title + last-update timestamp, drawn inside this view
        # (not as the host Labelframe's native text=) -- same pattern
        # CompactRunOverviewView/CompactRunsView use, where the host
        # Labelframe in main_window.py is given text="".
        header = ttk.Frame(self, style="CardPlain.TFrame")
        header.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Label(header, text="CHANNEL ACTIVITY", font=("Segoe UI", 8, "bold")).pack(side="left")

        updated_row = ttk.Frame(self, style="CardPlain.TFrame")
        updated_row.pack(fill="x", padx=6)
        self._updated_var = tk.StringVar(value="Updated at --:--:--")
        ttk.Label(updated_row, textvariable=self._updated_var, style="RailMuted.TLabel").pack(side="left")

        # The matrix is small and lives in a fixed-width sidebar rail, so it
        # always fits without scrolling -- a plain frame, no canvas/scrollbars.
        self._inner = ttk.Frame(self, style="CardPlain.TFrame")
        self._inner.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        self._build_matrix()

    # ------------------------------------------------------------------
    def _build_matrix(self) -> None:
        """Render one labelframe per box, each with 10 cell widgets and a legend."""
        # cleanup
        for child in list(self._inner.winfo_children()):
            child.destroy()
        self._cells.clear()

        globalindex = 1
        for col, box_id in enumerate(self._boxes):
            frame = ttk.Labelframe(self._inner, text=f"{box_id}", style="Rail.TLabelframe")
            frame.grid(row=0, column=col, padx=2, pady=3, sticky="n")

            for i in range(1, self._wells_per_box + 1):
                row = (i - 1) % 5
                col2 = 0 if i <= 5 else 1
                wid = f"{box_id}{globalindex}"
                # No number label: this matrix is a color-coded reference,
                # not a well picker, so the cell is just a plain swatch.
                cell = ttk.Label(
                    frame,
                    text="",
                    width=2,
                    style=view_utils.status_style_name("Idle"),
                )
                cell.grid(row=row, column=col2, padx=1, pady=1, sticky="nsew")
                self._cells[wid] = cell
                globalindex += 1

        self._build_legend(row=1, columnspan=max(len(self._boxes), 1))

    def _build_legend(self, *, row: int, columnspan: int) -> None:
        """Render a color legend below the channel boxes.

        "Queued" is a real, actively-used well status (cells still render
        its color via ``status_style_name``), but it's left out of this
        legend's display so the remaining statuses fit on one line.
        """
        legend = ttk.LabelFrame(self._inner, text="Legend", style="Rail.TLabelframe")
        legend.grid(row=row, column=0, columnspan=columnspan, padx=3, pady=(0, 4), sticky="ew")

        statuses = [s for s in view_utils.STATUS_COLORS if s != "Queued"]
        for index, status in enumerate(statuses):
            item = ttk.Frame(legend, style="CardPlain.TFrame")
            item.grid(row=0, column=index, padx=2, pady=2, sticky="w")

            color_box = ttk.Label(item, width=1, style=view_utils.status_style_name(status))
            color_box.pack(side="left", padx=(0, 2))
            ttk.Label(item, text=status, style="Rail.TLabel").pack(side="left")

    # ------------------------------------------------------------------
    def set_boxes(self, boxes: Iterable[BoxId]) -> None:
        """Replace visible box set and rebuild matrix widgets.

        Args:
            boxes: Ordered iterable of box ids to render.
        """
        self._boxes = list(boxes)
        self._build_matrix()

    def set_activity(self, mapping: Dict[WellId, str]) -> None:
        """Apply well-status colors to visible cells.

        Args:
            mapping: Mapping of ``well_id -> status``.
        """
        for wid, status in mapping.items():
            cell = self._cells.get(wid)
            if not cell:
                continue
            cell.configure(style=view_utils.status_style_name(status))

    def set_updated_at(self, text: str) -> None:
        """Update the timestamp label shown above the matrix.

        Args:
            text: Timestamp text, usually ``HH:MM:SS``.
        """
        label = (text or "").strip() or "--:--:--"
        self._updated_var.set(f"Updated at {label}")


if __name__ == "__main__":
    root = tk.Tk()
    view_utils.apply_theme(root)
    v = ChannelActivityView(root, boxes=("A","B","C","D"))
    v.pack(fill="both", expand=True)
    v.set_activity({"B11":"Running", "A2":"Error", "B10":"Done"})
    v.set_updated_at("12:03:15")
    root.mainloop()
