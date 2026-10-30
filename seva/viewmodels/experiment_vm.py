"""Experiment parameter editor state for ``ExperimentPanelView``.

Call context:
    ``seva/app/main.py`` creates :class:`ExperimentVM` and wires its methods to
    ``ExperimentPanelView`` callbacks. ``RunFlowPresenter`` later reads
    ``well_params`` to build typed plan requests for use cases.

Responsibilities:
    - Keep the currently edited flat form state (``fields``).
    - Persist grouped per-well snapshots for active electrochemistry modes.
    - Provide deterministic mode-specific copy/paste behavior.
    - Delegate mode validation and field ownership to ``ModeRegistry``.

Non-goals:
    - No network, filesystem, or adapter calls.
    - No use-case orchestration.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Set, Iterable
from seva.domain import WellId, ModeName
from seva.domain.modes import ModeRegistry

@dataclass
class ExperimentVM:
    """Own form state and per-well snapshots for experiment configuration.

    The view always writes and reads *flat* field ids (for example ``cv.scan``),
    while this view model stores persisted snapshots grouped by mode tokens
    (``CV``, ``AC``, ``DC``, ``CDL``, ``EIS``). Grouping keeps downstream plan
    construction predictable and aligned with mode-aware use cases.

    Attributes:
        on_start_requested: Optional callback used by higher-level glue.
        on_cancel_group: Optional callback for cancel actions.
        electrode_mode: Current electrode mode label (``2E`` or ``3E``).
        editing_well: Well currently shown in the editor.
        selection: Selected wells from the plate view.
        well_params: Per-well grouped snapshots persisted by ``save_params_for``.
        fields: Live flat form field store written by UI change handlers.
        clipboard_cv: Mode-scoped clipboard for CV fields.
        clipboard_dcac: Mode-scoped clipboard for DC/AC fields.
        clipboard_cdl: Mode-scoped clipboard for CDL fields.
        clipboard_eis: Mode-scoped clipboard for EIS fields.
        mode_registry: Central mode registry for validation and field filtering.
    """

    # Signals (optional)
    on_start_requested: Optional[Callable[[Dict], None]] = None
    on_cancel_group: Optional[Callable[[], None]] = None

    # UI/global
    electrode_mode: str = "3E"  # "2E" | "3E"
    editing_well: Optional[WellId] = None
    selection: Set[WellId] = field(default_factory=set)

    # Persistence: grouped params per well (only active modes present)
    well_params: Dict[WellId, Dict[ModeName, Dict[str, str]]] = field(default_factory=dict)

    # Live-Form-Store (flat)
    fields: Dict[str, str] = field(default_factory=dict)

    # Clipboards (flat)
    clipboard_cv: Dict[str, str] = field(default_factory=dict)
    clipboard_dcac: Dict[str, str] = field(default_factory=dict)
    clipboard_cdl: Dict[str, str] = field(default_factory=dict)
    clipboard_eis: Dict[str, str] = field(default_factory=dict)

    # Mode registry (centralized rules/labels/builders)
    mode_registry: ModeRegistry = field(default_factory=ModeRegistry.default)

    # ---------- Live form API ----------
    def set_field(self, field_id: str, value: str) -> None:
        """Store a single live form value from the editor view.

        Args:
            field_id: Flat field token used by the UI (for example ``run_cv``).
            value: Raw value string provided by the bound widget.

        Side Effects:
            Mutates ``self.fields`` in place.
        """
        self.fields[field_id] = value

    def set_selection(self, wells: Set[WellId]) -> None:
        """Replace the active well selection snapshot.

        Args:
            wells: Selected well ids from ``PlateVM`` / ``WellGridView``.

        Side Effects:
            Replaces ``self.selection`` with a copied set.
        """
        self.selection = set(wells)

    def set_electrode_mode(self, mode: str) -> None:
        """Validate and set the UI electrode mode token.

        Args:
            mode: Requested token, expected ``"2E"`` or ``"3E"``.

        Raises:
            ValueError: If ``mode`` is not one of the supported tokens.
        """
        if mode not in ("2E", "3E"):
            raise ValueError("Invalid ElectrodeMode")
        self.electrode_mode = mode

    # ---------- Copy helpers ----------
    def build_mode_snapshot_for_copy(self, mode: str) -> Dict[str, str]:
        """Build a mode-limited copy snapshot from the live form store.

        Not currently called from the composition root (the per-section
        copy/paste buttons that triggered this were removed from the view);
        kept as public ViewModel API for a future UI entry point.

        Args:
            mode: Mode token handled by ``ModeRegistry``.

        Returns:
            Dict[str, str]: Flat mapping containing only fields owned by ``mode``.
        """
        return self.mode_registry.filter_fields(mode, self.fields)

    # ---------- Persistenz (gruppiert) ----------
    def save_params_for(self, well_id: WellId, params: Dict[str, str]) -> None:
        """Persist grouped mode snapshots for one well.

        Usage scenario:
            Called after "Apply Params" in the experiment panel.

        Args:
            well_id: Well receiving the persisted snapshot.
            params: Flat field mapping from the editor.

        Side Effects:
            Updates ``self.well_params``. Removes the entry when no mode is active.
        """
        grouped = self._group_fields_by_mode(params or {})
        if grouped:
            self.well_params[well_id] = grouped
        else:
            # No active mode: remove stale well snapshot.
            self.well_params.pop(well_id, None)

    def get_params_for(self, well_id: WellId) -> Optional[Dict[str, str]]:
        """Return a flat view snapshot for one well.

        Args:
            well_id: Requested well id.

        Returns:
            Optional[Dict[str, str]]: Flat field map for UI hydration, including
            reconstructed mode toggle flags, or ``None`` when no snapshot exists.
        """
        raw = self.well_params.get(well_id)
        if not raw:
            return None
        return self._flatten_for_view(raw)

    def clear_params_for(self, well_id: WellId) -> None:
        """Remove persisted parameters for one well.

        Args:
            well_id: Target well id.
        """
        self.well_params.pop(well_id, None)

    def clear_all_params(self) -> None:
        """Remove all persisted well snapshots from the editor state.

        Not currently called from the composition root (the "Reset all
        Wells" button that triggered this was removed from the view); kept
        as public ViewModel API for a future UI entry point.
        """
        self.well_params.clear()

    # ---------- Commands ----------
    def cmd_copy_mode(
        self,
        mode: str,
        well_id: WellId,
        source_snapshot: Optional[Dict[str, str]] = None,
    ) -> None:
        """Copy mode-specific values into the mode clipboard.

        Not currently called from the composition root (see
        ``build_mode_snapshot_for_copy``).

        Args:
            mode: Mode token used to select clipboard and fields.
            well_id: Source well id (reserved for parity with command semantics).
            source_snapshot: Optional pre-filtered source mapping. When omitted,
                current live ``fields`` are used.

        Side Effects:
            Clears and rewrites the selected clipboard attribute.

        Raises:
            ValueError: Propagated from ``ModeRegistry.rule_for`` on bad mode.
        """
        rule = self.mode_registry.rule_for(mode)
        clipboard_attr = rule.clipboard_attr
        clipboard: Dict[str, str] = getattr(self, clipboard_attr)
        clipboard.clear()

        snapshot = (
            self.build_mode_snapshot_for_copy(mode)
            if source_snapshot is None
            else {
                fid: val
                for fid, val in source_snapshot.items()
                if self.mode_registry.is_mode_field(mode, fid)
            }
        )
        if not snapshot:
            return

        # Ensure mode-run flags are carried with clipboard payload so pasting
        # activates the same mode group on destination wells.
        for flag in rule.flags:
            snapshot[flag] = "1"
        clipboard.update(snapshot)

    def cmd_paste_mode(self, mode: str, well_ids: Iterable[WellId]) -> None:
        """Paste one mode clipboard into each target well snapshot.

        Not currently called from the composition root (see
        ``build_mode_snapshot_for_copy``).

        Args:
            mode: Mode token selecting clipboard and grouping behavior.
            well_ids: Destination well ids.

        Side Effects:
            Rewrites mode-specific subsets in ``self.well_params`` per well.

        Raises:
            ValueError: Propagated from ``ModeRegistry.rule_for`` on bad mode.
        """
        mode_key = (mode or "").upper()
        rule = self.mode_registry.rule_for(mode_key)
        clipboard_attr = rule.clipboard_attr
        clipboard: Dict[str, str] = getattr(self, clipboard_attr)
        if not clipboard:
            return

        for wid in well_ids:
            # Copy first so unrelated mode snapshots remain untouched.
            grouped = dict(self.well_params.get(wid, {}))

            if mode_key == "CV":
                sub = {k: v for k, v in clipboard.items() if k.startswith("cv.")}
                if sub:
                    grouped["CV"] = sub
                else:
                    grouped.pop("CV", None)

            elif mode_key == "DCAC":
                # DC separat
                if self._is_truthy(clipboard.get("run_dc")):
                    grouped["DC"] = self._extract_ea_params(
                        clipboard,
                        include_frequency=False,
                    )
                else:
                    grouped.pop("DC", None)
                # AC separat
                if self._is_truthy(clipboard.get("run_ac")):
                    grouped["AC"] = self._extract_ea_params(
                        clipboard,
                        include_frequency=True,
                    )
                else:
                    grouped.pop("AC", None)

            elif mode_key == "CDL":
                sub = {k: v for k, v in clipboard.items() if k.startswith("cdl.")}
                if sub:
                    grouped["CDL"] = sub
                else:
                    grouped.pop("CDL", None)

            elif mode_key == "EIS":
                sub = {k: v for k, v in clipboard.items() if k.startswith("eis.")}
                if sub:
                    grouped["EIS"] = sub
                else:
                    grouped.pop("EIS", None)

            self.well_params[wid] = grouped

    # ---------- Helpers ----------
    @staticmethod
    def _is_truthy(value: Any) -> bool:
        """Normalize mixed UI payload values to a boolean token."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _group_fields_by_mode(self, flat: Dict[str, str]) -> Dict[ModeName, Dict[str, str]]:
        """Convert flat form fields into grouped mode snapshots.

        Only modes with enabled run flags are emitted. This keeps persisted
        snapshots aligned with what the backend should execute.
        """
        run_cv  = self._is_truthy(flat.get("run_cv"))
        run_dc  = self._is_truthy(flat.get("run_dc"))
        run_ac  = self._is_truthy(flat.get("run_ac"))
        run_eis = self._is_truthy(flat.get("run_eis"))
        eval_cdl = self._is_truthy(flat.get("eval_cdl"))

        grouped: Dict[ModeName, Dict[str, str]] = {}

        if run_cv:
            grouped["CV"] = {k: v for k, v in flat.items() if k.startswith("cv.")}

        if run_dc:
            dc = self._extract_ea_params(flat, include_frequency=False)
            if dc:
                grouped["DC"] = dc

        if run_ac:
            ac = self._extract_ea_params(flat, include_frequency=True)
            if ac:
                grouped["AC"] = ac

        if eval_cdl:
            cdl = {k: v for k, v in flat.items() if k.startswith("cdl.")}
            if cdl:
                grouped["CDL"] = cdl

        if run_eis:
            eis = {k: v for k, v in flat.items() if k.startswith("eis.")}
            if eis:
                grouped["EIS"] = eis

        return grouped

    def _flatten_for_view(self, grouped: Dict[ModeName, Dict[str, str]]) -> Dict[str, str]:
        """Flatten grouped mode snapshots for UI hydration.

        Args:
            grouped: Mode-keyed snapshot map stored in ``self.well_params``.

        Returns:
            Dict[str, str]: Flat field map plus reconstructed run flags.
        """
        flat: Dict[str, str] = {}

        # CV
        if "CV" in grouped:
            flat.update(grouped["CV"])
        # DC/AC: For the shared ea.* fields we prefer AC (if present).
        if "AC" in grouped:
            flat.update(grouped["AC"])
        elif "DC" in grouped:
            flat.update(grouped["DC"])
        # CDL & EIS
        if "CDL" in grouped:
            flat.update(grouped["CDL"])
        if "EIS" in grouped:
            flat.update(grouped["EIS"])

        # Reconstruct flags for the view
        flat["run_cv"]   = "1" if "CV"  in grouped else "0"
        flat["run_dc"]   = "1" if "DC"  in grouped else "0"
        flat["run_ac"]   = "1" if "AC"  in grouped else "0"
        flat["eval_cdl"] = "1" if "CDL" in grouped else "0"
        flat["run_eis"]  = "1" if "EIS" in grouped else "0"

        return flat

    @staticmethod
    def _extract_ea_params(
        source: Mapping[str, str],
        *,
        include_frequency: bool,
    ) -> Dict[str, str]:
        """Extract electro-analysis fields for DC/AC snapshots.

        Args:
            source: Flat source mapping.
            include_frequency: ``True`` for AC snapshots, ``False`` for DC.

        Returns:
            Dict[str, str]: ``ea.*`` subset plus shared control fields.
        """
        params = {k: v for k, v in source.items() if k.startswith("ea.")}
        if not include_frequency:
            params.pop("ea.frequency_hz", None)
        if "control_mode" in source:
            params["control_mode"] = source["control_mode"]
        if "ea.target" in source:
            params["ea.target"] = source["ea.target"]
        return params
