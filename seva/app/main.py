"""GUI application bootstrap and top-level event wiring.

This module is the composition root of the desktop application. It assembles
the Tkinter user interface by wiring views, view models, controllers, use
cases, and concrete infrastructure adapters into one runnable app instance.

Responsibilities of this module include:

- Initializing the main window and core subviews (grid, experiment panel,
  run overview, channel activity, runs panel, settings dialog entrypoints)
- Creating and connecting view models to their corresponding views
- Binding user actions (toolbar commands, panel actions, hotkeys) to
  controller and use-case orchestration
- Selecting concrete runtime dependencies (local storage, discovery adapter,
  relay adapter) and connecting them to orchestration components
- Coordinating run-flow lifecycle integration (start, cancel, polling,
  download, registry sync)
- Keeping UI state synchronized with domain/application state snapshots
- Centralizing user-facing error formatting and status feedback

The ``App`` class intentionally contains no business logic. It orchestrates
interaction across layers and delegates domain behavior to use cases.

Application startup is performed via :func:`main`, which instantiates
``App`` and starts the Tkinter event loop.
"""

# seva/app/main.py
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
import time
from datetime import datetime
from tkinter import filedialog
from typing import Dict, Set, Optional, List, TYPE_CHECKING

if __package__ in (None, ""):
    # Support direct script execution (e.g., IDE "Run file") by ensuring the
    # repository root is importable before absolute package imports are used.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

# ---- Views (UI-only) ----
from seva.app.views.main_window import MainWindowView
from seva.app.views.well_grid_view import WellGridView
from seva.app.views.experiment_panel_view import ExperimentPanelView
from seva.app.views.run_overview_view import RunOverviewView
from seva.app.views.channel_activity_view import ChannelActivityView
from seva.app.views.settings_dialog import SettingsDialog
from seva.app.dataplotter_standalone import DataProcessingGUI
from seva.app.discovery_controller import DiscoveryController
from seva.app.settings_controller import SettingsController
from seva.app.download_controller import DownloadController
from seva.app.views.runs_panel_view import RunsPanelView

# ---- ViewModels ----
from seva.viewmodels.plate_vm import PlateVM
from seva.viewmodels.experiment_vm import ExperimentVM
from seva.viewmodels.progress_vm import ProgressVM
from seva.viewmodels.settings_vm import SettingsVM, BOX_IDS
from seva.viewmodels.live_data_vm import LiveDataVM
from seva.viewmodels.runs_vm import RunsVM

# ---- UseCases & Adapter ----
from seva.app.run_flow_presenter import RunFlowPresenter
from seva.adapters.storage_local import StorageLocal
from seva.adapters.discovery_http import HttpDiscoveryAdapter
from seva.adapters.relay_mock import RelayMock
from seva.usecases.save_plate_layout import SavePlateLayout
from seva.usecases.load_plate_layout import LoadPlateLayout
from seva.usecases.build_experiment_plan import BuildExperimentPlan
from seva.usecases.build_storage_meta import BuildStorageMeta
from seva.domain.runs_registry import RunsRegistry
from seva.domain.ports import UseCaseError
from seva.usecases.test_relay import TestRelay
from seva.usecases.set_electrode_mode import SetElectrodeMode
from seva.usecases.discover_devices import DiscoverDevices, MergeDiscoveredIntoRegistry
from seva.usecases.discover_and_assign_devices import DiscoverAndAssignDevices
from seva.utils import logging as logging_utils
from seva.app.controller import AppController

if TYPE_CHECKING:
    from seva.usecases.cancel_runs import CancelRuns

logging_utils.configure_root()


class App:
    """Composition root that wires UI, orchestration, and adapter dependencies."""

    def __init__(self) -> None:
        """Build the full desktop application composition graph.
        
        This constructor wires views, view models, controllers, adapters, and
        use cases, then connects callback chains so user actions can flow from
        UI widgets into orchestrated use-case calls.
        """
        self._log = logging.getLogger(__name__)
        # Main window with toolbar callback wiring
        self.win = MainWindowView(
            on_submit=self._on_submit,
            on_cancel_group=self._on_cancel_group,
            on_save_layout=self._on_save_layout,
            on_load_layout=self._on_load_layout,
            on_open_settings=self._on_open_settings,
            on_open_data_plotter=self._on_open_plotter,
        )
        self.win.bind("<Control-Shift-o>", self._on_open_download_folder_hotkey)
        self.win.bind("<Control-Shift-O>", self._on_open_download_folder_hotkey)

        # Registry is the long-lived state bridge between run-flow orchestration
        # and UI panels that need persisted/active run metadata.
        # ---- Shared registries ----
        self.runs = RunsRegistry.instance()

        # ---- ViewModels ----
        self.plate_vm = PlateVM(on_selection_changed=self._on_selection_changed)
        self.experiment_vm = ExperimentVM()
        self.progress_vm = ProgressVM(
            on_update_run_overview=self._apply_run_overview,
            on_update_channel_activity=self._apply_channel_activity,
        )
        self.settings_vm = SettingsVM()
        self._discovery_port = HttpDiscoveryAdapter(default_port=8000)
        self.uc_discover_devices = DiscoverDevices(self._discovery_port)
        self.uc_merge_discovered = MergeDiscoveredIntoRegistry()
        self.uc_discover_and_assign = DiscoverAndAssignDevices(
            self.uc_discover_devices,
            self.uc_merge_discovered,
        )
        self.live_vm = LiveDataVM()
        self.runs_vm = RunsVM(self.runs)

        # Views receive callbacks only; they do not call adapters/use cases directly.
        # ---- Subviews (constructor callbacks; no .configure(...)) ----
        initial_boxes = tuple(self._configured_boxes())
        self.wellgrid = WellGridView(
            self.win.wellgrid_host,
            boxes=initial_boxes,
            on_select_wells=lambda sel: self.plate_vm.set_selection(sel),
            on_copy_params_from=lambda wid: self.plate_vm.cmd_copy_from(wid),
            on_paste_params_to_selection=self.plate_vm.cmd_paste_to_selection,
            on_toggle_enable_selected=self.plate_vm.cmd_toggle_enable_selection,
            on_reset_selected=self._on_reset_well_config,
            on_reset_all=self._on_reset_all_wells,
            on_open_plot=lambda wid: self._open_plot_for_well(wid),
        )
        self.wellgrid.pack(fill="both", expand=True)
        self.win.mount_wellgrid(self.wellgrid)

        self.experiment = ExperimentPanelView(
            self.win.tab_experiment,
            on_change=lambda fid, val: self.experiment_vm.set_field(fid, val),
            on_toggle_control_mode=None,
            on_apply_params=self._on_apply_params,
            on_end_task=self._on_cancel_group,
            on_end_selection=self._on_end_selection,
            on_copy_cv=lambda: self._on_copy_mode("CV"),
            on_paste_cv=lambda: self._on_paste_mode("CV"),
            on_copy_dcac=lambda: self._on_copy_mode("DCAC"),
            on_paste_dcac=lambda: self._on_paste_mode("DCAC"),
            on_copy_cdl=lambda: self._on_copy_mode("CDL"),
            on_paste_cdl=lambda: self._on_paste_mode("CDL"),
            on_copy_eis=lambda: self._on_copy_mode("EIS"),
            on_paste_eis=lambda: self._on_paste_mode("EIS"),
            on_electrode_mode_changed=self._on_electrode_mode_changed,  # relay-only, not part of REST
        )
        self.win.mount_experiment_panel(self.experiment)

        self.run_overview = RunOverviewView(
            self.win.tab_run_overview,
            boxes=initial_boxes,
            on_cancel_group=self._on_cancel_group,
            on_download_group_results=self._on_download_group_results,
            on_download_box_results=self._on_download_box_results,
            on_open_plot=self._on_open_plotter,
        )
        self.win.mount_run_overview(self.run_overview)

        self.activity = ChannelActivityView(
            self.win.tab_channel_activity, boxes=initial_boxes
        )
        self.win.mount_channel_activity(self.activity)

        self.runs_panel = RunsPanelView(self.win.tabs)
        try:
            # Prefer inserting next to run overview tab for discoverability.
            idx = self.win.tabs.index(self.win.tab_run_overview) + 1
            self.win.tabs.insert(idx, self.runs_panel, text="Runs")
        except Exception:
            self.win.tabs.add(self.runs_panel, text="Runs")

        # Adapter wiring is lazy because URLs/API keys may be loaded from settings.
        # ---- REST Adapter & UseCases (lazy init after settings) ----
        self.controller = AppController(self.settings_vm)

        # ---- LocalStorage Adapter ----
        self._storage_root = os.environ.get("SEVA_STORAGE_ROOT") or "."
        self._storage = StorageLocal(root_dir=self._storage_root)
        self._load_user_settings()

        self.uc_save_layout = SavePlateLayout(self._storage)
        self.uc_load_layout = LoadPlateLayout(self._storage)
        self.uc_build_plan = BuildExperimentPlan()
        self.uc_build_storage_meta = BuildStorageMeta()
        self.discovery_controller = DiscoveryController(
            win=self.win,
            settings_vm=self.settings_vm,
            storage=self._storage,
            discovery_uc=self.uc_discover_and_assign,
            box_ids=BOX_IDS,
        )

        # ---- Relay Adapter & UseCases ----
        self._relay = RelayMock()
        self.uc_test_relay = TestRelay(self._relay)
        self.uc_set_electrode_mode = SetElectrodeMode(self._relay)

        # Presenter coordinates app-level flow and keeps Runs/Progress panels synchronized.
        # ---- Run flow coordination ----
        self.run_flow = RunFlowPresenter(
            win=self.win,
            controller=self.controller,
            runs=self.runs,
            runs_vm=self.runs_vm,
            progress_vm=self.progress_vm,
            settings_vm=self.settings_vm,
            storage=self._storage,
            plate_vm=self.plate_vm,
            experiment_vm=self.experiment_vm,
            runs_panel=self.runs_panel,
            ensure_adapter=self._ensure_adapter,
            toast_error=self._toast_error,
            build_plan=self.uc_build_plan,
            build_storage_meta=self.uc_build_storage_meta,
        )
        self.settings_controller = SettingsController(
            win=self.win,
            settings_vm=self.settings_vm,
            controller=self.controller,
            storage=self._storage,
            test_relay=self.uc_test_relay,
            ensure_adapter=self._ensure_adapter,
            toast_error=self._toast_error,
            on_discover_devices=self._on_discover_devices,
            apply_logging_preferences=self._apply_logging_preferences,
            apply_box_configuration=self._apply_box_configuration,
            stop_all_polling=self.run_flow.stop_all_polling,
        )
        self.download_controller = DownloadController(
            win=self.win,
            controller=self.controller,
            run_flow=self.run_flow,
            settings_vm=self.settings_vm,
            ensure_adapter=self._ensure_adapter,
            toast_error=self._toast_error,
        )
        self.runs_panel.on_select = self.run_flow.on_runs_select
        self.runs_panel.on_open = self.run_flow.on_runs_open_folder
        self.runs_panel.on_cancel = self.run_flow.on_runs_cancel
        self.runs_panel.on_delete = self.run_flow.on_runs_delete
        # Initial panel refresh ensures persisted entries are visible before user actions.
        self.run_flow.refresh_runs_panel()
        self.run_flow.configure_runs_registry(Path.home() / ".seva" / "runs_registry.json")
        self.run_flow.start_activity_polling()

        # ---- Initial UI state (demo-ish) ----
        self._seed_demo_state()
        self.win.set_status_message("Ready.")

    def _load_user_settings(self) -> None:
        """Load persisted settings, apply them to the SettingsVM, and sync UI.
        """
        try:
            payload = self._storage.load_user_settings()
        except Exception as exc:
            self.win.show_toast(f"Could not load settings: {exc}")
            return
        try:
            self.settings_vm.apply_dict(payload)
        except ValueError as exc:
            # Keep app startup resilient: show warning, keep defaults, continue.
            self.win.show_toast(str(exc))
        self._apply_logging_preferences()
        self._apply_box_configuration()

    def _apply_logging_preferences(self) -> None:
        """Apply GUI logging preferences and emit effective-level diagnostics.
        """
        level = logging_utils.apply_gui_preferences(self.settings_vm.debug_logging)
        self._log.debug(
            "Effective GUI log level: %s", logging_utils.level_name(level)
        )

    def _configured_boxes(self) -> List[str]:
        """Return configured box identifiers, falling back to defaults."""
        base_urls = self.settings_vm.api_base_urls or {}
        boxes = [
            str(box)
            for box, url in base_urls.items()
            if isinstance(url, str) and url.strip()
        ]
        if boxes:
            return sorted(boxes)
        return list(BOX_IDS)

    def _apply_box_configuration(self) -> None:
        """Update views to reflect the currently configured boxes."""
        boxes = self._configured_boxes()
        self.wellgrid.set_boxes(tuple(boxes))
        self.run_overview.set_boxes(boxes)
        self.activity.set_boxes(boxes)

    # ==================================================================
    # Adapter wiring
    # ==================================================================
    def _ensure_adapter(self) -> bool:
        """Ensure controller has adapters and use-cases initialized."""
        if self.controller.ensure_ready():
            return True
        # UX guardrail: all network actions depend on configured box URLs.
        self.win.show_toast("Configure box URLs in Settings first.")
        return False

    # ==================================================================
    # Demo data seeding (light)
    # ==================================================================
    def _seed_demo_state(self) -> None:
        """Initialize views with a neutral empty state after startup wiring.
        """
        boxes = tuple(self._configured_boxes())
        self.wellgrid.set_boxes(boxes)
        self.wellgrid.set_selection([])
        self.experiment.set_editing_well("-")
        self.experiment.set_electrode_mode("3E")
        # initial empty/idle overview
        self._apply_run_overview({"boxes": {}, "wells": [], "activity": {}})
        self.activity.set_updated_at("")

    # ==================================================================
    # Toolbar / Actions
    # ==================================================================
    def _on_submit(self) -> None:
        """Handle toolbar submit triggered by the user."""
        self.run_flow.start_run()

    def _on_cancel_group(self) -> None:
        """Forward toolbar cancel-group action to the run-flow presenter.
        """
        self.run_flow.cancel_active_group()

    def _on_end_selection(self) -> None:
        """Cancel only the currently selected runs through presenter orchestration.
        """
        self.run_flow.cancel_selected_runs()

    def _suggest_layout_filename(self) -> str:
        """Return a default layout filename derived from the experiment name."""
        raw_name = (getattr(self.settings_vm, "experiment_name", "") or "").strip()
        sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw_name)
        while "__" in sanitized:
            sanitized = sanitized.replace("__", "_")
        sanitized = sanitized.strip("_")
        if not sanitized:
            sanitized = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"layout_{sanitized}.json"

    def _on_save_layout(self) -> None:
        """Collect current plate/form state and persist it as a layout JSON file.
        """
        try:
            configured = self.plate_vm.configured()
            if not configured:
                self.win.show_toast("Nothing to save: no configured wells.")
                return

            selection = sorted(self.plate_vm.get_selection())
            if selection:
                try:
                    self.experiment_vm.set_selection(set(selection))
                except Exception:
                    pass

            default_name = self._suggest_layout_filename()
            initial_dir = os.path.abspath(self._storage_root)
            if not os.path.isdir(initial_dir):
                initial_dir = os.getcwd()
            path = filedialog.asksaveasfilename(
                parent=self.win,
                defaultextension=".json",
                filetypes=[("Layout JSON", "layout_*.json")],
                initialfile=default_name,
                initialdir=initial_dir,
                title="Save Layout",
            )
            if not path:
                return
            saved_path = self.uc_save_layout(  # type: ignore[misc]
                path,
                experiment_vm=self.experiment_vm,
                selection=selection,
            )
            self.win.show_toast(f"Saved {saved_path.name}")
        except Exception as e:
            self.win.show_toast(str(e))

    def _on_load_layout(self) -> None:
        """Load a saved layout and push restored state back into VMs and views.
        """
        try:
            initial_dir = os.path.abspath(self._storage_root)
            if not os.path.isdir(initial_dir):
                initial_dir = os.getcwd()
            path = filedialog.askopenfilename(
                parent=self.win,
                filetypes=[("Layout JSON", "layout_*.json")],
                initialdir=initial_dir,
                title="Load Layout",
            )
            if not path:
                return
            data = self.uc_load_layout(  # type: ignore[misc]
                path,
                experiment_vm=self.experiment_vm,
                plate_vm=self.plate_vm,
            )
            # Layout payload may omit explicit selection; fallback to configured wells.
            configured_wells = self.plate_vm.configured()
            selection_list = list(data.get("selection") or [])
            if not selection_list and configured_wells:
                selection_list = sorted(configured_wells)

            # Mirror electrode mode from VM into view
            try:
                self.experiment.set_electrode_mode(self.experiment_vm.electrode_mode)
            except Exception:
                pass

            # Propagate selection -> view gets updated via _on_selection_changed()
            self.plate_vm.set_selection(selection_list)
            self.wellgrid.set_configured_wells(configured_wells)
            self.wellgrid.set_selection(selection_list)
            # Idempotent safety sync to avoid stale form state in edge cases.
            self._on_selection_changed(set(selection_list))

            self.win.show_toast(f"Loaded {os.path.basename(path)}")
        except Exception as e:
            self.win.show_toast(str(e))

    def _on_discover_devices(self, dialog: Optional[SettingsDialog] = None) -> None:
        """Run discovery flow and surface failures as contextual UI toast.

        Args:
            dialog: Optional settings dialog to refresh with discovered URLs.
        """
        try:
            self.discovery_controller.discover(dialog)
        except Exception as exc:
            self._toast_error(exc, context="Discovery failed")

    def _on_open_settings(self) -> None:
        """Open the settings dialog through the dedicated settings controller.
        """
        self.settings_controller.open_dialog()


    def _on_open_plotter(self) -> None:
        """Open the standalone data plotter window.
        """
        DataProcessingGUI(self.win)

    def _on_download_group_results(self) -> None:
        """Trigger download for the active run group.
        """
        self.download_controller.download_group_results()

    def _on_download_box_results(self, box_id: str) -> None:
        """Trigger download action scoped from a box action in run overview.

        Args:
            box_id: Selected box identifier from the overview tab.

        The current implementation delegates to group-level download behavior.
        """
        self.download_controller.download_box_results(box_id)

    def _can_open_results_folder(self) -> bool:
        """Return whether this runtime platform supports opening folders directly.
        """
        if sys.platform.startswith("win"):
            return hasattr(os, "startfile")
        if sys.platform == "darwin":
            return True
        return shutil.which("xdg-open") is not None

    def _on_open_download_folder_hotkey(self, event=None):
        """Handle Ctrl+Shift+O and open the most recent download directory.

        Args:
            event: Tk keybinding event (unused).

        Returns:
            str: ``"break"`` to stop default key handling.
        """
        if not self.run_flow.last_download_dir:
            self.win.show_toast("Nothing downloaded yet.")
            return "break"
        if not self._can_open_results_folder():
            self.win.show_toast("Open folder not supported on this platform.")
            return "break"
        # Delegate to shared open logic to keep toolbar/hotkey behavior identical.
        self._open_results_folder(self.run_flow.last_download_dir)
        return "break"

    def _open_results_folder(self, path: str) -> None:
        """Open the download folder using the platform default file browser.

        Args:
            path: Absolute or relative directory path to open.
        """
        if not path or not os.path.isdir(path):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                opener = shutil.which("xdg-open")
                if not opener:
                    raise RuntimeError("xdg-open not available")
                subprocess.Popen([opener, path])
        except Exception as exc:
            self.win.show_toast(f"Could not open folder: {exc}")

    # ==================================================================
    # VM ↔ View glue
    # ==================================================================
    def _on_selection_changed(self, sel: Set[str]) -> None:
        """Sync experiment form state to current well selection.

        Args:
            sel: Selected well identifiers from ``PlateVM``.
        """
        # Clear the view first to avoid stale values
        self.experiment.clear_fields()

        if len(sel) == 1:
            wid = next(iter(sel))
            # Update the "editing well" label in the panel.
            try:
                self.experiment.set_editing_well(wid)
            except Exception:
                pass
            # Debug aid: show which physical device slot this button maps to.
            adapter = self.controller.job_adapter
            if adapter is not None:
                label = adapter.describe_well(wid)
                print(f"[well-select] {wid} -> {label or 'no device mapping yet'}")
            # Flatten grouped snapshot and set
            params = self.experiment_vm.get_params_for(wid)
            if params:
                # View always receives flat fields; VM handles grouped mode storage.
                self.experiment.set_fields(params)
        else:
            self.experiment.set_editing_well("–")

    def _on_apply_params(self) -> None:
        """Persist current form values into each selected well snapshot.

        A well is only marked "configured" when it actually has an active
        mode saved. Unchecking every mode (e.g. Run CV) and applying clears
        the well's configured/highlighted state instead of leaving it stuck.
        """
        selection = self.plate_vm.get_selection()
        if not selection:
            self.win.show_toast("No wells selected.")
            return
        # Save grouped per well (only active modes); save_params_for drops
        # the snapshot entirely when no mode is active.
        for wid in selection:
            self.experiment_vm.save_params_for(wid, self.experiment_vm.fields)

        now_configured = {wid for wid in selection if self.experiment_vm.get_params_for(wid)}
        now_unconfigured = selection - now_configured

        if now_configured:
            self.plate_vm.mark_configured(now_configured)
            self.wellgrid.add_configured_wells(now_configured)
        if now_unconfigured:
            self.plate_vm.clear_configured(now_unconfigured)
            self.wellgrid.clear_configured_wells(now_unconfigured)
        self.win.show_toast("Parameters applied.")

    def _on_reset_well_config(self) -> None:
        """Clear saved parameters only for currently selected wells.
        """
        selection = self.plate_vm.get_selection()
        if not selection:
            self.win.show_toast("No wells selected.")
            return

        for wid in selection:
            self.experiment_vm.clear_params_for(wid)

        self.plate_vm.clear_configured(selection)
        self.wellgrid.clear_configured_wells(selection)
        self._on_selection_changed(selection)
        self.win.show_toast("Well config reset.")

    def _on_reset_all_wells(self) -> None:
        """Clear all per-well parameter snapshots and reset selection state.
        """
        configured = self.plate_vm.configured()
        if not configured and not self.plate_vm.get_selection():
            self.win.show_toast("No wells to reset.")
            return

        self.experiment_vm.clear_all_params()
        self.plate_vm.clear_all_configured()
        self.plate_vm.set_selection([])
        self.wellgrid.clear_all_configured()
        self.wellgrid.set_selection([])
        self.win.show_toast("All wells reset.")

    def _mode_label(self, mode: str) -> str:
        """Return user-facing label text for a normalized mode token.

        Args:
            mode: Normalized mode token such as ``CV`` or ``EIS``.
        """
        return self.experiment_vm.mode_registry.label_for(mode)

    def _on_copy_mode(self, mode: str) -> None:
        """Copy mode-specific form values from one selected source well.

        Args:
            mode: Mode token copied from current form values.
        """
        selection = self.plate_vm.get_selection()
        if len(selection) == 0:
            self.win.show_toast("Select one well to copy.")
            return
        if len(selection) > 1:
            self.win.show_toast("Select exactly one well to copy.")
            return
        # Copy is intentionally single-source to keep clipboard semantics deterministic.
        well_id = next(iter(selection))
        try:
            snapshot = self.experiment_vm.build_mode_snapshot_for_copy(mode)
        except Exception as e:
            self.win.show_toast(str(e))
            return
        if not snapshot:
            self.win.show_toast("No params in form.")
            return
        try:
            self.experiment_vm.cmd_copy_mode(mode, well_id, source_snapshot=snapshot)
            self.win.show_toast(f"Copied {self._mode_label(mode)}.")
        except Exception as e:
            self.win.show_toast(str(e))

    def _on_paste_mode(self, mode: str) -> None:
        """Paste mode-specific clipboard values into all selected wells.

        Args:
            mode: Mode token to paste into selected wells.
        """
        selection = self.plate_vm.get_selection()
        if not selection:
            self.win.show_toast("No wells selected.")
            return

        clipboard_attr = {
            "CV": "clipboard_cv",
            "DCAC": "clipboard_dcac",
            "CDL": "clipboard_cdl",
            "EIS": "clipboard_eis",
        }.get((mode or "").upper())
        clipboard = getattr(self.experiment_vm, clipboard_attr, {})
        if not clipboard:
            self.win.show_toast("Clipboard empty.")
            return

        try:
            self.experiment_vm.cmd_paste_mode(mode, selection)
        except Exception as e:
            self.win.show_toast(str(e))
            return

        # Pasting implicitly configures targets; reflect that in VM and grid styling.
        self.plate_vm.mark_configured(selection)
        self.wellgrid.add_configured_wells(selection)
        self._on_selection_changed(selection)
        self.win.show_toast(f"Pasted {self._mode_label(mode)}.")

    def _on_electrode_mode_changed(self, mode: str) -> None:
        """Apply electrode-mode change to VM first, then relay use case.

        Args:
            mode: Normalized electrode mode token (``2E`` or ``3E``).
        """
        try:
            self.experiment_vm.set_electrode_mode(mode)
            self.uc_set_electrode_mode(mode)
            self.win.show_toast(f"Electrode mode: {mode}")
        except Exception as e:
            self.win.show_toast(str(e))

    def _apply_run_overview(self, dto: Dict) -> None:
        """Render presenter-provided run overview DTO into the overview view.

        Args:
            dto: View DTO with box metadata and well table rows.
        """
        boxes = dto.get("boxes", {}) or {}
        # DTO is presenter-owned; this method only adapts shape for concrete widgets.
        for b, meta in boxes.items():
            # subrun may be a CSV string or a list; normalize to CSV for the view
            sub = meta.get("subrun")
            if isinstance(sub, list):
                sub = ", ".join(sub)
            self.run_overview.set_box_status(
                b,
                phase=meta.get("phase", "Idle"),
                progress_pct=meta.get("progress", 0),
                sub_run_id=sub,
            )
        self.run_overview.set_well_rows(dto.get("wells", []) or [])

    def _apply_channel_activity(self, mapping: Dict[str, str]) -> None:
        """Render channel activity map and updated-at label in the activity view.

        Args:
            mapping: Mapping of ``well_id -> status`` produced by ``ProgressVM``.
        """
        self.activity.set_activity(mapping)
        label = self.progress_vm.updated_at_label or time.strftime("%H:%M:%S")
        self.activity.set_updated_at(label)

    def _open_plot_for_well(self, well_id: str) -> None:
        """Placeholder plot action hook for per-well interactions in the grid.

        Args:
            well_id: Well identifier selected by the user.
        """
        self.win.show_toast(f"Open PNG for {well_id}")

    # ==================================================================
    # Error handling helpers
    # ==================================================================

    def _toast_error(self, err: Exception, *, context: Optional[str] = None) -> None:
        """Format an exception into UI-safe text and show it in the toast area.

        Args:
            err: Exception raised by orchestration/usecase layers.
            context: Optional prefix for contextualizing the error.
        """
        message = self._format_error_message(err)
        if context:
            message = f"{context}: {message}"
        self.win.show_toast(message)

    def _format_error_message(self, err: Exception) -> str:
        """Convert exceptions into user-facing text while logging diagnostics.

        Args:
            err: Exception to convert.
        """
        if isinstance(err, UseCaseError):
            self._log.warning("UseCase error (%s): %s", err.code, err.message)
            return err.message
        self._log.exception("Unexpected error")
        return str(err)

    


def main() -> None:
    """Start the desktop application entrypoint."""
    app = App()
    app.win.mainloop()


if __name__ == "__main__":
    main()
