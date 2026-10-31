"""Experiment parameter form view used by the Experiment tab.

The panel renders grouped mode sections (CV, EA, CDL, EIS) and emits only UI
callbacks. It performs no adapter calls, persistence, or business validation.
"""
from __future__ import annotations
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional, Dict

from seva.app.views import view_utils



class ExperimentPanelView(ttk.Frame):
    """Tkinter frame that renders experiment parameter sections.
    
    Attributes:
        Members are consumed by controllers, presenters, or Tk views.
    """
    OnVoid = Optional[Callable[[], None]]
    OnChange = Optional[Callable[[str, str], None]]

    _CONTROL_MODE_VALUES = ("mA", "V")

    def __init__(
        self,
        parent: tk.Widget,
        *,
        on_change: OnChange = None,
        on_toggle_control_mode: OnVoid = None,
        on_apply_params: OnVoid = None,
        on_end_task: OnVoid = None,
    ) -> None:
        """Build experiment form sections and wire field callbacks.

        Args:
            parent: Notebook tab container.
            on_change: Callback ``(field_id, value)`` for any field change.
            on_toggle_control_mode: Reserved callback for mode toggle actions.
            on_apply_params: Callback for "Update Parameters".
            on_end_task: Callback for "End Task".
        """
        super().__init__(parent)

        self._on_change = on_change
        self._on_toggle_control_mode = on_toggle_control_mode
        self._on_apply_params = on_apply_params
        self._on_end_task = on_end_task

        self._vars: Dict[str, tk.StringVar] = {}
        self._flag_vars: Dict[str, tk.BooleanVar] = {}
        self._mode_frames: Dict[str, ttk.Labelframe] = {}
        self._field_defaults: Dict[str, str] = {}

        # --- Layout -----------------------------------------------------
        # No column/row weight on self: the 2x2 grid as a whole keeps its
        # natural (compact) size instead of stretching to fill the scroll
        # host, so entries don't balloon across the full window. Within
        # that grid, each card uses sticky="nsew" so it fills its own
        # cell -- and since a grid column's/row's width/height is always
        # the natural max across every cell in it, this makes CDL match
        # CV's width (same column), EIS match DCAC's width (same column),
        # and CV/DCAC match each other's height (same row), all without
        # hardcoding a size.

        # CV
        self.cv_run_var = tk.BooleanVar(value=False)
        cv = ttk.Labelframe(self, text="Cyclic Voltammetry (CV)", style="Card.TLabelframe")
        self._mode_frames["CV"] = cv
        cv.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self._make_section_header(cv, check_var=self.cv_run_var, check_text="Run CV")
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
        dcac = ttk.Labelframe(self, text="Electrolysis (DC/AC)", style="Card.TLabelframe")
        self._mode_frames["EA"] = dcac
        dcac.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        tools = ttk.Frame(dcac, style="CardPlain.TFrame")
        tools.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(tools, text="Run DC", variable=self.dc_run_var, style="Card.TCheckbutton").pack(side="left")
        ttk.Checkbutton(tools, text="Run AC", variable=self.ac_run_var, style="Card.TCheckbutton").pack(side="left", padx=(8, 0))

        self._make_labeled_entry(dcac, "Duration (s)", "ea.duration_s", 1)
        self._make_labeled_entry(dcac, "Charge Cutoff (C)", "ea.charge_cutoff_c", 2)
        self._make_labeled_entry(dcac, "Voltage Cutoff (V)", "ea.voltage_cutoff_v", 3)
        self._make_labeled_entry(dcac, "Frequency (Hz)", "ea.frequency_hz", 4)

        # Run DC and Run AC are mutually exclusive: checking one unchecks
        # the other. Registered before _register_flag's own traces so the
        # exclusivity resolves first, and the EA-enabled/border update that
        # follows always sees the final, consistent dc/ac state.
        self.dc_run_var.trace_add("write", lambda *_: self.ac_run_var.set(False)
                                   if self.dc_run_var.get() and self.ac_run_var.get() else None)
        self.ac_run_var.trace_add("write", lambda *_: self.dc_run_var.set(False)
                                   if self.ac_run_var.get() and self.dc_run_var.get() else None)

        self._register_flag("run_dc", self.dc_run_var)
        self._register_flag("run_ac", self.ac_run_var)

        # Editable dropdown: the field can be typed into (not grayed out
        # like a readonly combobox), but on focus-out/Enter anything other
        # than one of the listed values snaps back to the last valid one.
        # "current (mA)" is the restored default (registered in
        # _field_defaults so clear_fields() doesn't blank it).
        ttk.Label(dcac, text="Control Mode", style="CardMuted.TLabel").grid(row=5, column=0, padx=6, pady=4, sticky="w")
        self.control_mode_var = tk.StringVar(value="mA")
        self._control_mode_last_valid = self.control_mode_var.get()
        vcmd = (self.register(self._validate_control_mode), "%P")
        self.control_combo = ttk.Combobox(
            dcac, textvariable=self.control_mode_var, width=6,
            values=self._CONTROL_MODE_VALUES,
            validate="focusout", validatecommand=vcmd,
        )
        self.control_combo.grid(row=5, column=1, padx=6, pady=4, sticky="w")
        self.control_combo.bind("<Return>", lambda e: self.control_combo.validate())
        self.control_combo.bind("<<ComboboxSelected>>", lambda e: self.control_combo.validate())
        self._vars["control_mode"] = self.control_mode_var
        self._field_defaults["control_mode"] = "mA"
        self.control_mode_var.trace_add("write",
            lambda *_: self._on_change and self._on_change("control_mode", self.control_mode_var.get()))

        # Plain text entry, not a dropdown: Target is a free-form numeric
        # value, so it uses the same labeled-entry pattern as the other
        # DC/AC fields instead of a Combobox.
        self._make_labeled_entry(dcac, "Target", "ea.target", 6)
        self.target_var = self._vars["ea.target"]

        # CDL
        self.cdl_eval_var = tk.BooleanVar(value=False)
        cdl = ttk.Labelframe(self, text="Capacitance (Cdl)", style="Card.TLabelframe")
        self._mode_frames["CDL"] = cdl
        cdl.grid(row=1, column=0, padx=6, pady=6, sticky="nsew")
        self._make_section_header(cdl, check_var=self.cdl_eval_var, check_text="Evaluate Cdl")
        self._make_labeled_entry(cdl, "Vertex A vs. OCP (V)", "cdl.vertex_a_v", 1)
        self._make_labeled_entry(cdl, "Vertex B vs. OCP (V)", "cdl.vertex_b_v", 2)
        self._register_flag("eval_cdl", self.cdl_eval_var)

        # EIS
        self.eis_run_var = tk.BooleanVar(value=False)
        eis = ttk.Labelframe(self, text="Impedance (EIS)", style="Card.TLabelframe")
        self._mode_frames["EIS"] = eis
        eis.grid(row=1, column=1, padx=6, pady=6, sticky="nsew")
        self._make_section_header(eis, check_var=self.eis_run_var, check_text="Run EIS")
        self._make_labeled_entry(eis, "Freq Start (Hz)", "eis.freq_start_hz", 1)
        self._make_labeled_entry(eis, "Freq End (Hz)", "eis.freq_end_hz", 2)
        self._register_flag("run_eis", self.eis_run_var)

        # Toggle sections initially
        self._set_section_enabled("CV",  bool(self.cv_run_var.get()))
        self._set_section_enabled("EA",  bool(self.dc_run_var.get()) or bool(self.ac_run_var.get()))
        self._set_section_enabled("EIS", bool(self.eis_run_var.get()))
        self._set_section_enabled("CDL", bool(self.cdl_eval_var.get()))

        # Footer: each button is gridded directly onto self's own row 0/1
        # columns (not a separate 50/50-split footer frame), so End Task
        # inherits column 0's width (shared with CV/CDL) and Update
        # Parameters inherits column 1's width (shared with DCAC/EIS) --
        # same column-width-matching mechanism as the four cards above.
        ttk.Button(self, text="End Task", command=self._confirm_end_task).grid(
            row=2, column=0, sticky="ew", padx=6, pady=(4, 6)
        )
        ttk.Button(self, text="Update Parameters", command=self._on_apply_params).grid(
            row=2, column=1, sticky="ew", padx=6, pady=(4, 6)
        )

    # --- helpers -------------------------------------------------------
    def _confirm_end_task(self) -> None:
        """Ask for confirmation before forwarding the "End Task" action.

        A simple UI-only confirmation (no business logic), so an accidental
        click doesn't end the task; only calls through to the injected
        on_end_task callback if the user confirms.
        """
        if messagebox.askyesno("End Task", "End the current task?", parent=self):
            if self._on_end_task:
                self._on_end_task()

    def _validate_control_mode(self, proposed: str) -> bool:
        """Accept a typed Control Mode value only if it's one of the
        allowed options; otherwise snap back to the last valid value.

        Args:
            proposed: The field's content at focus-out/Enter (Tk's "%P").
        """
        if proposed in self._CONTROL_MODE_VALUES:
            self._control_mode_last_valid = proposed
            return True
        self.control_mode_var.set(self._control_mode_last_valid)
        return False

    def _make_section_header(
        self, parent: tk.Widget, *, check_var: tk.BooleanVar, check_text: str
    ) -> None:
        """Create section header row with just the enable checkbox.

        Args:
            parent: Section frame receiving the header.
            check_var: Checkbox variable for section enable state.
            check_text: Checkbox label text.
        """
        left = ttk.Frame(parent, style="CardPlain.TFrame"); left.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(left, text=check_text, variable=check_var, style="Card.TCheckbutton").pack(side="left")

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
        ttk.Label(parent, text=label, style="CardMuted.TLabel").grid(row=row, column=0, padx=6, pady=2, sticky="w")
        self._field_defaults[field_id] = default
        var = tk.StringVar(value=default); self._vars[field_id] = var
        ent = ttk.Entry(parent, textvariable=var, width=6); ent.grid(row=row, column=1, padx=6, pady=2, sticky="w")
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
        frame.configure(style="CardActive.TLabelframe" if enabled else "Card.TLabelframe")
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
    )
    view.pack(fill="both", expand=True)

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
            "control_mode": "mA",
            "ea.target": "12.5",
            "eval_cdl": True,
            "cdl.vertex_a_v": "0.10",
            "cdl.vertex_b_v": "0.30",
            "run_eis": True,
            "eis.freq_start_hz": "100000",
            "eis.freq_end_hz": "0.5",
        }
    )

    root.mainloop()
