"""Well selection grid view used by the experiment setup tab.

The grid renders one button per slot/well and exposes callback events for
selection, context-menu actions, and plot opening. It owns only UI state.
"""

from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Iterable, Optional, Sequence, Set

from seva.app.views import view_utils

WellId = str
BoxId = str


class WellGridView(ttk.Frame):
    """Grid of wells grouped by boxes (A–D)."""

    # Callback types
    OnSelect = Optional[Callable[[Set[WellId]], None]]
    OnVoid = Optional[Callable[[], None]]
    OnWell = Optional[Callable[[WellId], None]]

    def __init__(
        self,
        parent: tk.Widget,
        *,
        boxes: Sequence[BoxId] = ("A", "B", "C", "D"),
        wells_per_box: int = 10,
        on_select_wells: OnSelect = None,
        on_toggle_enable_selected: OnVoid = None,
        on_copy_params_from: OnWell = None,
        on_paste_params_to_selection: OnVoid = None,
        on_reset_selected: OnVoid = None,
        on_reset_all: OnVoid = None,
        on_open_plot: OnWell = None,
        on_electrode_mode_changed: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Construct grid view and bind callbacks.

        Args:
            parent: Parent container for the grid view.
            boxes: Ordered list of box identifiers.
            wells_per_box: Number of wells rendered per box.
            on_select_wells: Selection callback with selected well-id set.
            on_toggle_enable_selected: Context menu toggle callback.
            on_copy_params_from: Context menu copy-source callback.
            on_paste_params_to_selection: Context menu paste callback.
            on_reset_selected: Context menu reset callback for selected wells.
            on_reset_all: Toolbar callback for resetting all wells.
            on_open_plot: Double-click/context callback for per-well plotting.
            on_electrode_mode_changed: Callback receiving normalized ``2E/3E``.
        """
        super().__init__(parent, style="CardPlain.TFrame")

        self._boxes = list(boxes)
        self._wells_per_box = int(wells_per_box)
        self._on_select_wells = on_select_wells
        self._on_toggle_enable_selected = on_toggle_enable_selected
        self._on_copy_params_from = on_copy_params_from
        self._on_paste_params_to_selection = on_paste_params_to_selection
        self._on_reset_selected = on_reset_selected
        self._on_reset_all = on_reset_all
        self._on_open_plot = on_open_plot
        self._on_electrode_mode_changed = on_electrode_mode_changed
        self._electrode_display = tk.StringVar(value="3-electrode")

        # State
        self._buttons: Dict[WellId, tk.Button] = {}
        self._selected: Set[WellId] = set()
        self._configured: Set[WellId] = set()  # wells with assigned params

        self._build_ui()

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """Build toolbar and box/well button matrix."""
        toolbar = ttk.Frame(self, style="CardPlain.TFrame")
        toolbar.pack(fill="x", pady=(0, 4))
        # Three equal-weight columns so the two reset buttons and the
        # electrode-mode dropdown are evenly spaced across the full width.
        for col in range(3):
            toolbar.columnconfigure(col, weight=1)

        ttk.Button(
            toolbar,
            text="Reset Well Config",
            command=self._on_reset_selected,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            toolbar,
            text="Reset all Wells",
            command=self._on_reset_all,
        ).grid(row=0, column=1, sticky="")

        mode_frame = ttk.Frame(toolbar, style="CardPlain.TFrame")
        mode_frame.grid(row=0, column=2, sticky="e")
        ttk.Label(mode_frame, text="Electrode Mode:", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        self._mode_combo = ttk.Combobox(
            mode_frame, textvariable=self._electrode_display,
            state="readonly", values=["3-electrode", "2-electrode"], width=13,
        )
        self._mode_combo.pack(side="left")
        self._mode_combo.bind("<<ComboboxSelected>>", lambda e: self._emit_electrode_mode())

        # Borderless: the Plate Labelframe (see main_window.py) already
        # draws the card boundary, so this inner frame shouldn't add a
        # second, redundant border around the box groups.
        grid = ttk.Frame(self, style="CardPlain.TFrame")
        grid.pack(fill="both", expand=True)
        for col in range(len(self._boxes)):
            grid.columnconfigure(col, weight=1)

        global_index = 1
        for col, box in enumerate(self._boxes):
            lf = ttk.Labelframe(grid, text=f"{box}", style="Card.TLabelframe")
            lf.grid(row=0, column=col, padx=6, pady=4, sticky="n")
            for i in range(1, self._wells_per_box + 1):
                r = (i - 1) % 5
                c = 0 if i <= 5 else 1
                # Well ids are globally indexed (A1..A10, B11..B20, ...).
                wid = f"{box}{global_index}"
                btn = ttk.Button(lf, text=str(global_index), style=view_utils.well_style_name(selected=False, configured=False))
                btn.grid(row=r, column=c, padx=2, pady=1, sticky="nsew")
                btn.bind("<Button-1>", lambda e, w=wid: self._on_click(e, w))
                btn.bind("<Double-Button-1>", lambda e, w=wid: self._on_open_plot(w))
                btn.bind("<Button-3>", lambda e, w=wid: self._context_menu(e, w))
                self._buttons[wid] = btn
                global_index += 1

    # ------------------------------------------------------------------
    # Public API used by ViewModel
    # ------------------------------------------------------------------
    def set_boxes(self, boxes: Iterable[BoxId]) -> None:
        """Rebuild grid for a new set of boxes.

        Args:
            boxes: Ordered iterable of box ids to render.
        """
        self._boxes = list(boxes)
        for child in list(self.winfo_children()):
            child.destroy()
        self._buttons.clear()
        self._selected.clear()
        self._configured.clear()
        self._build_ui()
        self._emit_selection()

    def set_configured_wells(self, wells: Iterable[WellId]) -> None:
        """Replace the configured set and recolor buttons (green).

        Args:
            wells: Well ids that should be marked configured.
        """
        self._configured = set(wells)
        self._repaint_all()

    def add_configured_wells(self, wells: Iterable[WellId]) -> None:
        """Add wells to configured set and repaint only affected buttons.

        Args:
            wells: Well ids to mark configured.
        """
        self._configured.update(wells)
        self._repaint_some(wells)

    def clear_configured_wells(self, wells: Iterable[WellId]) -> None:
        """Remove configured state for specific wells and repaint them.

        Args:
            wells: Well ids to clear from configured state.
        """
        for w in wells:
            self._configured.discard(w)
        self._repaint_some(wells)

    def clear_all_configured(self) -> None:
        """Clear configured state for all wells and repaint full grid."""
        self._configured.clear()
        self._repaint_all()

    def set_electrode_mode(self, mode: str) -> None:
        """Set electrode-mode combobox from normalized token.

        Args:
            mode: Normalized token (``2E`` or ``3E``).
        """
        self._electrode_display.set("2-electrode" if mode == "2E" else "3-electrode")

    def get_electrode_mode(self) -> str:
        """Return normalized electrode token (``2E`` or ``3E``)."""
        disp = (self._electrode_display.get() or "").strip()
        return "2E" if disp.startswith("2") else "3E"

    def _emit_electrode_mode(self) -> None:
        """Convert display label to normalized token and emit callback."""
        disp = (self._electrode_display.get() or "").strip()
        mode = "2E" if disp.startswith("2") else "3E"
        if self._on_electrode_mode_changed:
            self._on_electrode_mode_changed(mode)

    def get_selection(self) -> Set[WellId]:
        """Return a copy of the current selection set."""
        return set(self._selected)

    def set_selection(self, wells: Iterable[WellId]) -> None:
        """Replace selected wells and notify selection callback.

        Args:
            wells: Well ids to mark as selected.
        """
        self._selected = set(wells)
        self._repaint_all()
        self._emit_selection()

    # ------------------------------------------------------------------
    # Styling (no run statuses here)
    # ------------------------------------------------------------------
    def _apply_style(self, wid: WellId) -> None:
        """Apply the ttk style for a single well based on current state.

        Background reflects "configured" (has saved params) independently
        from the border, which reflects "selected" — so a well can show
        both states at once instead of selection masking configured. Uses
        one of the four named button styles view_utils registers, rather
        than per-widget raw colors.

        Args:
            wid: Well identifier to repaint.
        """
        btn = self._buttons.get(wid)
        if not btn:
            return
        style_name = view_utils.well_style_name(
            selected=wid in self._selected, configured=wid in self._configured,
        )
        btn.configure(style=style_name)

    # ------------------------------------------------------------------
    # Selection & helpers
    # ------------------------------------------------------------------
    def _on_click(self, event: tk.Event, well_id: str) -> None:
        """Handle click/shift-click selection behavior for one well button.

        Args:
            event: Tk click event.
            well_id: Well identifier of clicked button.
        """
        shift = bool(event.state & 0x0001)  # ShiftMask
        if shift:
            if well_id in self._selected:
                self._selected.remove(well_id)
            else:
                self._selected.add(well_id)
        else:
            self._selected = {well_id}
        self._repaint_all()
        self._emit_selection()
    
    def _toggle_select(self, well_id: WellId) -> None:
        """Toggle one well in selection set and emit callback.

        Args:
            well_id: Well identifier to toggle.
        """
        if well_id in self._selected:
            self._selected.remove(well_id)
        else:
            self._selected.add(well_id)
        self._apply_style(well_id)
        self._emit_selection()

    def _repaint_some(self, wells: Iterable[WellId]) -> None:
        """Repaint only a subset of wells.

        Args:
            wells: Well ids that need style refresh.
        """
        for w in wells:
            self._apply_style(w)

    def _repaint_all(self) -> None:
        """Repaint style for every well button."""
        for w in self._buttons.keys():
            self._apply_style(w)

    def _emit_selection(self) -> None:
        """Emit full selection set to the injected callback."""
        if self._on_select_wells:
            self._on_select_wells(set(self._selected))

    def _context_menu(self, event: tk.Event, well_id: WellId) -> None:
        """Open well button context menu with copy/paste/reset actions.

        Args:
            event: Tk right-click event.
            well_id: Well identifier under cursor.
        """
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy Params from", command=lambda: self._on_copy_params_from(well_id))
        menu.add_command(label="Paste Params to Selection", command=self._on_paste_params_to_selection)
        menu.add_separator()
        menu.add_command(label="Enable/Disable Selection", command=self._on_toggle_enable_selected)
        menu.add_command(label="Reset Selection", command=self._on_reset_selected)
        menu.add_separator()
        menu.add_command(label="Open PNG", command=lambda: self._on_open_plot(well_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    root.title("WellGridView Demo")
    root.geometry("980x520")
    view_utils.apply_theme(root)

    grid = WellGridView(
        root,
        boxes=("A", "B", "C", "D"),
        on_select_wells=lambda wells: print(f"[demo] selected wells: {sorted(wells)}"),
        on_toggle_enable_selected=lambda: print("[demo] toggle enable for selected wells"),
        on_copy_params_from=lambda well: print(f"[demo] copy params from {well}"),
        on_paste_params_to_selection=lambda: print("[demo] paste params to selection"),
        on_reset_selected=lambda: print("[demo] reset selected wells"),
        on_reset_all=lambda: print("[demo] reset all wells"),
        on_open_plot=lambda well: print(f"[demo] open plot for {well}"),
    )
    grid.pack(fill="both", expand=True)

    grid.set_configured_wells(
        {
            "A1",
            "A2",
            "A3",
            "A4",
            "A5",
            "A6",
            "A7",
            "A8",
            "A9",
            "A10",
            "B11",
            "B12",
            "B13",
            "B14",
            "C23",
            "D36",
        }
    )
    grid.set_selection({"A2", "B11", "C23"})

    root.mainloop()
