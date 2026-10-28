"""Experiment parameter form view used by the Experiment tab.

The panel renders grouped mode sections (CV, EA, CDL, EIS) and emits only UI
callbacks. It performs no adapter calls, persistence, or business validation.
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Dict



class ExperimentPanelView(ttk.Frame):
    """Tkinter frame that renders experiment parameter sections.
    
    Attributes:
        Members are consumed by controllers, presenters, or Tk views.
    """
    OnVoid = Optional[Callable[[], None]]
    OnChange = Optional[Callable[[str, str], None]]

    def __init__(
        self,
        parent: tk.Widget,
        *,
        on_change: OnChange = None,
        on_toggle_control_mode: OnVoid = None,
        on_apply_params: OnVoid = None,
        on_end_task: OnVoid = None,
        on_end_selection: OnVoid = None,
        on_copy_cv: OnVoid = None,
        on_paste_cv: OnVoid = None,
        on_copy_dcac: OnVoid = None,
        on_paste_dcac: OnVoid = None,
        on_copy_cdl: OnVoid = None,
        on_paste_cdl: OnVoid = None,
        on_copy_eis: OnVoid = None,
        on_paste_eis: OnVoid = None,
        on_electrode_mode_changed: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Build experiment form sections and wire field callbacks.

        Args:
            parent: Notebook tab container.
            on_change: Callback ``(field_id, value)`` for any field change.
            on_toggle_control_mode: Reserved callback for mode toggle actions.
            on_apply_params: Callback for "Update Parameters".
            on_end_task: Callback for "End Task".
            on_end_selection: Callback for "End Selection".
            on_copy_cv: Copy callback for CV section.
            on_paste_cv: Paste callback for CV section.
            on_copy_dcac: Copy callback for EA section.
            on_paste_dcac: Paste callback for EA section.
            on_copy_cdl: Copy callback for CDL section.
            on_paste_cdl: Paste callback for CDL section.
            on_copy_eis: Copy callback for EIS section.
            on_paste_eis: Paste callback for EIS section.
            on_electrode_mode_changed: Callback receiving normalized ``2E/3E``.
        """
        super().__init__(parent)

        self._on_change = on_change
        self._on_toggle_control_mode = on_toggle_control_mode
        self._on_apply_params = on_apply_params
        self._on_end_task = on_end_task
        self._on_end_selection = on_end_selection
        self._on_copy_cv = on_copy_cv
        self._on_paste_cv = on_paste_cv
        self._on_copy_dcac = on_copy_dcac
        self._on_paste_dcac = on_paste_dcac
        self._on_copy_cdl = on_copy_cdl
        self._on_paste_cdl = on_paste_cdl
        self._on_copy_eis = on_copy_eis
        self._on_paste_eis = on_paste_eis
        self._on_electrode_mode_changed = on_electrode_mode_changed

        self._electrode_display = tk.StringVar(value="3-electrode")
        self._vars: Dict[str, tk.StringVar] = {}
        self._flag_vars: Dict[str, tk.BooleanVar] = {}
        self._mode_frames: Dict[str, ttk.Labelframe] = {}
        self._field_defaults: Dict[str, str] = {}

        # --- Layout -----------------------------------------------------
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        # CV
        self.cv_run_var = tk.BooleanVar(value=False)
        cv = ttk.Labelframe(self, text="Cyclic Voltammetry (CV)")
        self._mode_frames["CV"] = cv
        cv.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self._make_header_with_tools(cv, check_var=self.cv_run_var, check_text="Run CV",
                                     on_copy=self._on_copy_cv, on_paste=self._on_paste_cv)
        self._make_labeled_entry(cv, "Vertex 1 vs. Ref (V)", "cv.vertex1_v", 1)
        self._make_labeled_entry(cv, "Vertex 2 vs. Ref (V)", "cv.vertex2_v", 2)
        self._make_labeled_entry(cv, "Final vs. Ref (V)", "cv.final_v", 3)
        self._make_labeled_entry(cv, "Scan Rate (V/s)", "cv.scan_rate_v_s", 4)
        self._make_labeled_entry(cv, "Cycle Count", "cv.cycles", 5)
        self._make_labeled_entry(cv, "Sampling Rate (Hz)", "cv.sampling_rate_hz", 6, default="100")
        self._register_flag("run_cv", self.cv_run_var)

        # DC/AC
        self.dc_run_var = tk.BooleanVar(value=False)
        self.ac_run_var = tk.BooleanVar(value=False)
        dcac = ttk.Labelframe(self, text="Electrolysis (DC/AC)")
        self._mode_frames["EA"] = dcac
        dcac.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        tools = ttk.Frame(dcac)
        tools.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(tools, text="Run DC", variable=self.dc_run_var).pack(side="left")
        ttk.Checkbutton(tools, text="Run AC", variable=self.ac_run_var).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="⎘", width=3, command=self._on_copy_dcac).pack(side="right")
        ttk.Button(tools, text="🗀", width=3, command=self._on_paste_dcac).pack(side="right")

        self._make_labeled_entry(dcac, "Duration (s)", "ea.duration_s", 1)
        self._make_labeled_entry(dcac, "Charge Cutoff (C)", "ea.charge_cutoff_c", 2)
        self._make_labeled_entry(dcac, "Voltage Cutoff (V)", "ea.voltage_cutoff_v", 3)
        self._make_labeled_entry(dcac, "Frequency (Hz)", "ea.frequency_hz", 4)
        self._register_flag("run_dc", self.dc_run_var)
        self._register_flag("run_ac", self.ac_run_var)

        ttk.Label(dcac, text="Control Mode").grid(row=5, column=0, padx=6, pady=4, sticky="w")
        self.control_mode_var = tk.StringVar(value="current (mA)")
        self.control_combo = ttk.Combobox(
            dcac, textvariable=self.control_mode_var, state="readonly",
            values=["current (mA)", "potential (V)"]
        )
        self.control_combo.grid(row=5, column=1, padx=6, pady=4, sticky="ew")

        ttk.Label(dcac, text="Target").grid(row=6, column=0, padx=6, pady=4, sticky="w")
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(dcac, textvariable=self.target_var)
        self.target_combo.grid(row=6, column=1, padx=6, pady=4, sticky="ew")

        # Register control_mode & ea.target as "normal" fields
        self._vars["control_mode"] = self.control_mode_var
        self.control_mode_var.trace_add("write",
            lambda *_: self._on_change and self._on_change("control_mode", self.control_mode_var.get()))
        self._vars["ea.target"] = self.target_var
        self.target_var.trace_add("write",
            lambda *_: self._on_change and self._on_change("ea.target", self.target_var.get()))

        # CDL
        self.cdl_eval_var = tk.BooleanVar(value=False)
        cdl = ttk.Labelframe(self, text="Capacitance (Cdl)")
        self._mode_frames["CDL"] = cdl
        cdl.grid(row=1, column=0, padx=6, pady=6, sticky="nsew")
        self._make_header_with_tools(cdl, check_var=self.cdl_eval_var, check_text="Evaluate Cdl",
                                     on_copy=self._on_copy_cdl, on_paste=self._on_paste_cdl)
        self._make_labeled_entry(cdl, "Vertex A vs. Ref (V)", "cdl.vertex_a_v", 1)
        self._make_labeled_entry(cdl, "Vertex B vs. Ref (V)", "cdl.vertex_b_v", 2)
        self._register_flag("eval_cdl", self.cdl_eval_var)

        # EIS
        self.eis_run_var = tk.BooleanVar(value=False)
        eis = ttk.Labelframe(self, text="Impedance (EIS)")
        self._mode_frames["EIS"] = eis
        eis.grid(row=1, column=1, padx=6, pady=6, sticky="nsew")
        self._make_header_with_tools(eis, check_var=self.eis_run_var, check_text="Run EIS",
                                     on_copy=self._on_copy_eis, on_paste=self._on_paste_eis)
        self._make_labeled_entry(eis, "Freq Start (Hz)", "eis.freq_start_hz", 1)
        self._make_labeled_entry(eis, "Freq End (Hz)", "eis.freq_end_hz", 2)
        self._make_labeled_entry(eis, "Points", "eis.points", 3)
        self._make_labeled_entry(eis, "Spacing (log/lin)", "eis.spacing", 4)
        self._register_flag("run_eis", self.eis_run_var)

        # Toggle sections initially
        self._set_section_enabled("CV",  bool(self.cv_run_var.get()))
        self._set_section_enabled("EA",  bool(self.dc_run_var.get()) or bool(self.ac_run_var.get()))
        self._set_section_enabled("EIS", bool(self.eis_run_var.get()))
        self._set_section_enabled("CDL", bool(self.cdl_eval_var.get()))

        # Footer
        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 6))
        for idx in range(4):
            footer.columnconfigure(idx, weight=1)
        self.editing_well_var = tk.StringVar(value="–")
        ttk.Label(footer, textvariable=self.editing_well_var).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Update Parameters", command=self._on_apply_params).grid(
            row=0, column=1, sticky="", padx=6
        )
        ttk.Button(footer, text="End Selection", command=self._on_end_selection).grid(
            row=0, column=2, sticky="e", padx=6
        )
        ttk.Button(footer, text="End Task", command=self._on_end_task).grid(
            row=0, column=3, sticky="e", padx=6
        )

        mode_box = ttk.Labelframe(footer, text="Electrode Mode")
        mode_box.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 6))
        self._mode_combo = ttk.Combobox(
            mode_box, textvariable=self._electrode_display,
            state="readonly", values=["3-electrode", "2-electrode"], width=16
        )
        self._mode_combo.pack(padx=6, pady=4)
        self._mode_combo.bind("<<ComboboxSelected>>", lambda e: self._emit_electrode_mode())

    # --- helpers -------------------------------------------------------
    def _emit_electrode_mode(self) -> None:
        """Convert display label to normalized token and emit callback."""
        disp = (self._electrode_display.get() or "").strip()
        mode = "2E" if disp.startswith("2") else "3E"
        if self._on_electrode_mode_changed:
            self._on_electrode_mode_changed(mode)

    def _make_header_with_tools(
        self, parent: tk.Widget, *, check_var: tk.BooleanVar, check_text: str,
        on_copy: OnVoid, on_paste: OnVoid
    ) -> None:
        """Create section header row with enable checkbox and copy/paste tools.

        Args:
            parent: Section frame receiving the header.
            check_var: Checkbox variable for section enable state.
            check_text: Checkbox label text.
            on_copy: Copy callback for section clipboard action.
            on_paste: Paste callback for section clipboard action.
        """
        row = 0
        left = ttk.Frame(parent); left.grid(row=row, column=0, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(left, text=check_text, variable=check_var).pack(side="left")
        right = ttk.Frame(parent); right.grid(row=row, column=1, sticky="e", padx=6, pady=4)
        ttk.Button(right, text="⎘", width=3, command=on_copy).pack(side="right")
        ttk.Button(right, text="🗀", width=3, command=on_paste).pack(side="right")

    def _make_labeled_entry(
        self, parent: tk.Widget, label: str, field_id: str, row: int, default: str = ""
    ) -> None:
        """Create one labeled entry and bind variable trace to ``on_change``.

        Args:
            parent: Section frame receiving the entry row.
            label: User-facing label text.
            field_id: Field identifier emitted to ``on_change``.
            row: Grid row index in the section frame.
            default: Initial value, and the value restored by ``clear_fields()``
                for a well with no saved snapshot (e.g. a conventional default
                sampling rate so it doesn't need retyping every time).
        """
        ttk.Label(parent, text=label).grid(row=row, column=0, padx=6, pady=2, sticky="w")
        self._field_defaults[field_id] = default
        var = tk.StringVar(value=default); self._vars[field_id] = var
        ent = ttk.Entry(parent, textvariable=var); ent.grid(row=row, column=1, padx=6, pady=2, sticky="ew")
        parent.columnconfigure(1, weight=1)
        def _on_var_changed(*_):
            """Forward traced entry updates to the external change callback."""
            if self._on_change:
                self._on_change(field_id, var.get())
        var.trace_add("write", _on_var_changed)

    def _register_flag(self, field_id: str, var: tk.BooleanVar) -> None:
        """Register boolean field and keep section enablement in sync.

        Args:
            field_id: Boolean field id emitted to ``on_change``.
            var: Tk boolean variable backing checkbox state.
        """
        # <- BUGFIX: register flag so clear_fields()/set_fields() apply
        self._flag_vars[field_id] = var

        def _apply():
            """Emit boolean change and refresh section enabled states."""
            # 1) notify VM
            if self._on_change:
                self._on_change(field_id, "1" if var.get() else "0")
            # 2) toggle section
            if field_id == "run_cv":
                self._set_section_enabled("CV", bool(var.get()))
            elif field_id == "run_dc":
                self._set_section_enabled("EA", bool(var.get()) or bool(self.ac_run_var.get()))
            elif field_id == "run_ac":
                self._set_section_enabled("EA", bool(var.get()) or bool(self.dc_run_var.get()))
            elif field_id == "run_eis":
                self._set_section_enabled("EIS", bool(var.get()))
            elif field_id == "eval_cdl":
                self._set_section_enabled("CDL", bool(var.get()))

        _apply()
        var.trace_add("write", lambda *_: _apply())

    def _set_section_enabled(self, section_key: str, enabled: bool) -> None:
        """Enable or disable editable widgets inside one mode section.

        Args:
            section_key: Mode section key (for example ``CV``).
            enabled: Target enabled state.
        """
        frame = self._mode_frames.get(section_key)
        if not frame:
            return
        state = "normal" if enabled else "disabled"

        def _walk(w):
            """Recursively toggle entry/combobox state in a section frame.

            Args:
                w: Root widget to traverse.
            """
            for child in w.winfo_children():
                try:
                    if isinstance(child, (ttk.Entry, ttk.Combobox)):
                        child.configure(state=state)
                except Exception:
                    pass
                _walk(child)
        _walk(frame)

    # --- Public setters ------------------------------------------------
    def set_editing_well(self, well_label: str) -> None:
        """Update footer label showing which well is currently edited.

        Args:
            well_label: Well label string to render.
        """
        self.editing_well_var.set(f"Editing Well: {well_label}")

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

    def set_fields(self, mapping: Dict[str, str]) -> None:
        """Apply field/flag values from viewmodel snapshot mapping.

        Args:
            mapping: Field-id to value mapping from viewmodel.
        """
        if not mapping:
            return
        # Text fields
        for fid, var in self._vars.items():
            if fid in mapping:
                val = mapping.get(fid)
                var.set("" if val is None else str(val))
        # Flags – ALLE bekannten Flags deterministisch setzen
        def _as_bool(v: object) -> bool:
            """Convert persisted field value into checkbox boolean state.

            Args:
                v: Raw persisted value.
            """
            if isinstance(v, bool):
                return v
            return str(v or "").strip().lower() in ("1", "true", "yes", "on")
        for fid, var in self._flag_vars.items():
            var.set(_as_bool(mapping.get(fid, False)))

    def clear_fields(self) -> None:
        """Reset all text and boolean inputs to defaults."""
        for field_id, var in self._vars.items():
            var.set(self._field_defaults.get(field_id, ""))
        for var in self._flag_vars.values():
            var.set(False)


if __name__ == "__main__":
    def _log_change(field_id: str, value: str) -> None:
        print(f"[demo] field changed: {field_id}={value}")

    def _log_action(name: str):
        return lambda: print(f"[demo] action: {name}")

    root = tk.Tk()
    root.title("ExperimentPanelView Demo")
    root.geometry("980x720")

    view = ExperimentPanelView(
        root,
        on_change=_log_change,
        on_toggle_control_mode=_log_action("toggle_control_mode"),
        on_apply_params=_log_action("apply_params"),
        on_end_task=_log_action("end_task"),
        on_end_selection=_log_action("end_selection"),
        on_copy_cv=_log_action("copy_cv"),
        on_paste_cv=_log_action("paste_cv"),
        on_copy_dcac=_log_action("copy_dcac"),
        on_paste_dcac=_log_action("paste_dcac"),
        on_copy_cdl=_log_action("copy_cdl"),
        on_paste_cdl=_log_action("paste_cdl"),
        on_copy_eis=_log_action("copy_eis"),
        on_paste_eis=_log_action("paste_eis"),
        on_electrode_mode_changed=lambda mode: print(f"[demo] electrode mode: {mode}"),
    )
    view.pack(fill="both", expand=True)

    view.set_editing_well("B14")
    view.set_electrode_mode("2E")
    view.set_fields(
        {
            "run_cv": True,
            "cv.vertex1_v": "0.95",
            "cv.vertex2_v": "-0.20",
            "cv.final_v": "0.10",
            "cv.scan_rate_v_s": "0.05",
            "cv.cycles": "3",
            "run_dc": True,
            "run_ac": False,
            "ea.duration_s": "1800",
            "ea.charge_cutoff_c": "1.2",
            "ea.voltage_cutoff_v": "2.4",
            "ea.frequency_hz": "50",
            "control_mode": "current (mA)",
            "ea.target": "12.5",
            "eval_cdl": True,
            "cdl.vertex_a_v": "0.10",
            "cdl.vertex_b_v": "0.30",
            "run_eis": True,
            "eis.freq_start_hz": "100000",
            "eis.freq_end_hz": "0.5",
            "eis.points": "45",
            "eis.spacing": "log",
        }
    )

    root.mainloop()
