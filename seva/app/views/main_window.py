"""
MainWindowView
---------------
Tkinter main window for the SEVA GUI following MVVM + Hexagonal architecture.
This file contains **only View code** – no HTTP, no domain logic. It exposes
callback hooks that are expected to be connected to ViewModels.

Author: (you)
Notes:
- Keep all comments in English (per project guidance).
- The window provides:
  * Toolbar with core actions
  * Left area for WellGridView (inserted later)
  * Right Notebook with tabs: Experiment, Run Overview, Channel Activity
  * StatusBar at the bottom
- All external interactions are signaled via callbacks passed to the constructor.
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from seva.app.views import view_utils


class MainWindowView(tk.Tk):
    """Top-level application window.

    This class is UI-only. It defines layout containers and wires UI events to
    callbacks provided by ViewModels (or a simple presenter). The child views
    (WellGridView, ExperimentPanelView, RunOverviewView, ChannelActivityTab)
    will be created and inserted later.
    """

    # ---- Callback type aliases (callables injected from ViewModels) ----
    OnVoid = Optional[Callable[[], None]]

    def __init__(
        self,
        *,
        on_submit: OnVoid = None,
        on_save_layout: OnVoid = None,
        on_load_layout: OnVoid = None,
        on_open_settings: OnVoid = None,
        on_open_data_plotter: OnVoid = None,
    ) -> None:
        """Create root window, static layout regions, and key bindings.

        Args:
            on_submit: Callback for run-start action.
            on_save_layout: Callback for layout save action.
            on_load_layout: Callback for layout load action.
            on_open_settings: Callback for opening settings dialog.
            on_open_data_plotter: Callback for opening data plotter window.
        """
        super().__init__()

        # ---- Window basics ----
        self.title("SEVA – Schindler Electrolysis and Voltammetry Analysis")
        # No explicit geometry(): the window shrink-wraps to its actual
        # content size on first draw. A hardcoded geometry here previously
        # fought with minsize (750 vs 1000 wide), forcing the window wider
        # than its content and leaving Plate's "ew"-stretched border out of
        # alignment with the compact experiment panel below it.
        self.minsize(820, 600)
        view_utils.apply_theme(self)
        self.configure(bg=view_utils.PAGE_BG)
        self._logo_image = view_utils.load_logo_image()

        # Keep references to callbacks (can be None; we guard before calling)
        self._on_submit = on_submit
        self._on_save_layout = on_save_layout
        self._on_load_layout = on_load_layout
        self._on_open_settings = on_open_settings
        self._on_open_data_plotter = on_open_data_plotter

        # ---- High-level layout: 3 rows (Toolbar, Main, Status) ----
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self._build_toolbar(self)
        self._build_main_area(self)
        self._build_statusbar(self)

        # Keyboard shortcuts (lightweight, can be extended)
        self.bind("<Control-Return>", lambda e: self._on_submit and self._on_submit())
        self.bind("<Control-s>", lambda e: self._on_save_layout and self._on_save_layout())
        self.bind("<Control-o>", lambda e: self._on_load_layout and self._on_load_layout())

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------
    def _build_toolbar(self, parent: tk.Widget) -> None:
        """Build the top toolbar and wire button callbacks.

        Args:
            parent: Root container hosting the toolbar row.
        """
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        toolbar.columnconfigure(tuple(range(10)), weight=0)

        col = 0
        if self._logo_image is not None:
            ttk.Label(toolbar, image=self._logo_image).grid(row=0, column=col, padx=(0, 12))
            col += 1

        # Left-to-right order: Settings, Save Plate, Load Plate, Data
        # Plotter, Start (the primary action, accent-styled, trailing).
        ttk.Button(toolbar, text="Settings", command=self._on_open_settings).grid(
            row=0, column=col, padx=(0, 6)
        )
        col += 1
        ttk.Button(toolbar, text="Save Plate", command=self._on_save_layout).grid(
            row=0, column=col, padx=6
        )
        col += 1
        ttk.Button(toolbar, text="Load Plate", command=self._on_load_layout).grid(
            row=0, column=col, padx=6
        )
        col += 1
        ttk.Button(toolbar, text="Data Plotter", command=self._on_open_data_plotter).grid(
            row=0, column=col, padx=6
        )
        col += 1
        ttk.Button(
            toolbar, text="Start", style="Accent.TButton", command=self._on_submit
        ).grid(row=0, column=col, padx=(75, 0))

    # ------------------------------------------------------------------
    # Main Area (split: left WellGrid, right Notebook)
    # ------------------------------------------------------------------
    def _build_main_area(self, parent: tk.Widget) -> None:
        """Build the dashboard layout: full-width Plate, then a center
        Experiment panel beside a persistent status rail.

        No tabs: Run Overview and Channel Activity are always visible in the
        status rail (compact form); Runs and the full Run Overview open as
        dialogs on demand (see run_overview_dialog.py / runs_dialog.py).

        Args:
            parent: Root container hosting the main area row.
        """
        content = ttk.Frame(parent)
        content.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        content.rowconfigure(1, weight=1)
        content.columnconfigure(0, weight=1)

        # Row 0: Plate — stretched ("ew") to match the toolbar and body rows
        # below it, all of which share this same content column. Since none
        # of the three rows is hardcoded wider than its own natural content
        # (see the window's minsize/geometry change in __init__), they all
        # converge on one consistent width -- the widest row's natural
        # width -- so every frame's left/right border lines up.
        self.wellgrid_host = ttk.Labelframe(content, text="Plate", style="Card.TLabelframe")
        self.wellgrid_host.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # Row 1: center (Experiment) + right status rail
        body = ttk.Frame(content)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        # Column 0 (experiment panel) is NOT given weight: it shrink-wraps
        # to its compact content width (see _make_scroll_host) instead of
        # stretching to fill the window, which previously left a large
        # empty gap between the compact cards and the status rail.

        # The experiment panel's four mode cards can exceed the available
        # body height (e.g. a short window, or every section expanded at
        # once); wrap it in a scroll host so content is always reachable
        # instead of being silently clipped by the fixed-height body row.
        exp_outer, self.tab_experiment = self._make_scroll_host(body)
        exp_outer.grid(row=0, column=0, sticky="ns", padx=(0, 8))

        self.status_rail_host = ttk.Frame(body, width=300)
        self.status_rail_host.grid(row=0, column=1, sticky="ns")
        self.status_rail_host.grid_propagate(False)

        # Run Overview and Recent Runs are pinned to the same fixed height
        # (with pack_propagate(False) so content can't stretch them) so the
        # two cards visually match; Recent Runs gets an internal scrollbar
        # (see CompactRunsView) since its row count can exceed that height.
        RAIL_CARD_HEIGHT = 100

        self.run_overview_rail_host = ttk.Labelframe(
            self.status_rail_host, text="", style="Rail.TLabelframe", height=RAIL_CARD_HEIGHT
        )
        self.run_overview_rail_host.pack(fill="x", pady=(0, 10))
        self.run_overview_rail_host.pack_propagate(False)

        self.runs_rail_host = ttk.Labelframe(
            self.status_rail_host, text="", style="Rail.TLabelframe", height=RAIL_CARD_HEIGHT
        )
        self.runs_rail_host.pack(fill="x", pady=(0, 10))
        self.runs_rail_host.pack_propagate(False)

        self.activity_rail_host = ttk.Labelframe(
            self.status_rail_host, text="Channel Activity", style="Rail.TLabelframe"
        )
        self.activity_rail_host.pack(fill="x", pady=(0, 10))


    def _make_scroll_host(self, parent: tk.Widget):
        """
        Create a scrollable host frame using Canvas + inner Frame pattern.
        
        Args:
            parent: Container that receives the scroll host.

        Returns (outer, inner):
        - outer: frame to grid in the layout (contains canvas + scrollbars)
        - inner: content frame where children (WellGridView) are packed/gridded
        """
        outer = ttk.Frame(parent)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0, bg=view_utils.PAGE_BG)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_configure(event):
            """Refresh scroll region when the canvas itself is resized.

            Args:
                event: Tk configure event from the canvas.
            """
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)

        def _on_inner_configure(event):
            """Shrink-wrap canvas width to the inner frame's natural width
            and refresh the scroll region, so the host is only ever as wide
            as its (compact) content instead of stretching to fill leftover
            space in the body row.

            Args:
                event: Tk configure event from inner frame.
            """
            canvas.configure(width=inner.winfo_reqwidth(), scrollregion=canvas.bbox("all"))

        def _on_mousewheel(event):
            """Scroll canvas vertically on mouse wheel movement.

            Args:
                event: Tk mouse wheel event.
            """
            delta = -1 * (event.delta // 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        def _bind_wheel(_event):
            """Capture mouse-wheel scrolling only while the pointer is over
            this scroll host, so it doesn't hijack scrolling elsewhere in
            the window (e.g. the status rail)."""
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(_event):
            canvas.unbind_all("<MouseWheel>")

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_configure)
        outer.bind("<Enter>", _bind_wheel)
        outer.bind("<Leave>", _unbind_wheel)

        return outer, inner

    # ------------------------------------------------------------------
    # StatusBar
    # ------------------------------------------------------------------
    def _build_statusbar(self, parent: tk.Widget) -> None:
        """Build statusbar with the editing-well indicator, relay badges,
        and toast label.

        Args:
            parent: Root container hosting the status row.
        """
        status = ttk.Frame(parent)
        status.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 8))
        status.columnconfigure(1, weight=1)

        self.editing_well_var = tk.StringVar(value="Editing Well: –")
        ttk.Label(status, textvariable=self.editing_well_var).grid(row=0, column=0, sticky="w")

        # Relay area (badges per box; created on first use)
        self._relay_area = ttk.Frame(status)
        self._relay_area.grid(row=0, column=2, sticky="w", padx=(12, 0))
        self._relay_labels = {}  # type: dict[str, ttk.Label]

        self.status_message_var = tk.StringVar(value="Ready.")
        ttk.Label(status, textvariable=self.status_message_var).grid(row=0, column=3, sticky="e")

    # ------------------------------------------------------------------
    # Public API (called by VMs/presenters)
    # ------------------------------------------------------------------
    def set_status_message(self, text: str) -> None:
        """Update the short status message shown in the status bar.

        Args:
            text: Message text to render.
        """
        self.status_message_var.set(text)

    def set_editing_well(self, well_label: str) -> None:
        """Update the statusbar label showing which well is currently edited.

        Args:
            well_label: Well label string to render.
        """
        self.editing_well_var.set(f"Editing Well: {well_label}")

    def mount_wellgrid(self, view: tk.Widget) -> None:
        """Mount the WellGridView into the left scroll host.

        Args:
            view: Grid widget created by caller.

        Usage: create your WellGridView with parent=self.wellgrid_host.
        """
        # The host returns a frame; children can be gridded/packed by the caller.
        # We keep this API for clarity; nothing to do here for now.
        pass

    def set_relay_status(self, box_id: str, status: str) -> None:
        """Update relay reachability indicator for one box.

        Args:
            box_id: Box identifier.
            status: Status label text (for example ``OK`` or ``Fail``).
        """
        # Create label lazily
        if box_id not in self._relay_labels:
            lbl = ttk.Label(self._relay_area, text=f"{box_id}: ?")
            lbl.pack(side="left", padx=4)
            self._relay_labels[box_id] = lbl

        lbl = self._relay_labels[box_id]
        text = f"{box_id}: {status}"
        lbl.configure(text=text)

    def show_toast(self, message: str, level: str = "info") -> None:
        """Show lightweight feedback in the statusbar.

        Args:
            message: Message text.
            level: Reserved level token for future styling.
        """
        self.status_message_var.set(message)

    def mount_experiment_panel(self, view: tk.Widget) -> None:
        """Replace placeholder with the real ExperimentPanel view.

        Args:
            view: View widget to mount into experiment tab.
        """
        for child in list(self.tab_experiment.winfo_children()):
            if child is view:
                continue
            child.destroy()
        # Pack only if not already managed
        try:
            view.pack_info()
        except Exception:
            view.pack(fill="both", expand=True)

    def mount_run_overview_compact(self, view: tk.Widget) -> None:
        """Mount the compact run-overview summary into the status rail.

        Args:
            view: CompactRunOverviewView widget to mount.
        """
        for child in list(self.run_overview_rail_host.winfo_children()):
            if child is view:
                continue
            child.destroy()
        try:
            view.pack_info()
        except Exception:
            view.pack(fill="x")

    def mount_channel_activity(self, view: tk.Widget) -> None:
        """Mount the ChannelActivity view into the status rail.

        Args:
            view: View widget to mount into the activity rail host.
        """
        for child in list(self.activity_rail_host.winfo_children()):
            if child is view:
                continue
            child.destroy()
        try:
            view.pack_info()
        except Exception:
            view.pack(fill="both", expand=True)

    def mount_recent_runs(self, view: tk.Widget) -> None:
        """Mount the compact recent-runs summary into the status rail.

        Args:
            view: CompactRunsView widget to mount.
        """
        for child in list(self.runs_rail_host.winfo_children()):
            if child is view:
                continue
            child.destroy()
        try:
            view.pack_info()
        except Exception:
            view.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

if __name__ == "__main__":
    # Manual preview with visible demo state; regular defaults stay unchanged.
    win = MainWindowView(
        on_submit=lambda: print("[demo] Start clicked"),
        on_save_layout=lambda: print("[demo] Save Layout clicked"),
        on_load_layout=lambda: print("[demo] Load Layout clicked"),
        on_open_settings=lambda: print("[demo] Settings clicked"),
        on_open_data_plotter=lambda: print("[demo] Data Plotter clicked"),
    )
    win.set_editing_well("B14")
    win.set_relay_status("A", "Connected")
    win.set_relay_status("B", "Timeout")
    win.set_relay_status("C", "Connected")
    win.set_status_message("Demo: Run active on 2/3 boxes, polling every 750 ms.")
    win.show_toast("Demo ready: toolbar callbacks print to console.")
    win.mainloop()
