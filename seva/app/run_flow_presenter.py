"""Presenter that coordinates run lifecycle workflows for the desktop app.

The presenter owns start/cancel/poll/download orchestration and keeps run
registry state synchronized with UI viewmodels. It does not render widgets.
"""

from __future__ import annotations


import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple
from tkinter import messagebox

from seva.domain.entities import PlanMeta
from seva.domain.ports import UseCaseError
from seva.domain.util import well_id_to_box
from seva.domain.runs_registry import RunsRegistry
from seva.domain.storage_meta import StorageMeta
from seva.usecases.run_flow_coordinator import (
    FlowHooks,
    FlowTick,
    GroupContext,
    RunFlowCoordinator,
)
from seva.usecases.start_experiment_batch import StartBatchResult
from seva.usecases.build_experiment_plan import (
    BuildExperimentPlan,
    ExperimentPlanRequest,
    ModeSnapshot,
    WellSnapshot,
)
from seva.usecases.build_storage_meta import BuildStorageMeta
from seva.viewmodels.experiment_vm import ExperimentVM
from seva.viewmodels.plate_vm import PlateVM
from seva.viewmodels.progress_vm import ProgressVM
from seva.viewmodels.runs_vm import RunsVM
from seva.viewmodels.settings_vm import SettingsVM
from seva.app.polling_scheduler import PollingScheduler

from seva.app.controller import AppController
from seva.adapters.storage_local import StorageLocal


@dataclass
class FlowSession:
    """Runtime wiring for one tracked run group.

    Attributes:
        coordinator: Stateful run-flow coordinator instance.
        context: Group context produced by coordinator start/attach calls.
    """

    coordinator: RunFlowCoordinator
    context: GroupContext


class RunFlowPresenter:
    """Coordinate start/cancel/poll logic and run registry updates."""

    def __init__(
        self,
        *,
        win,
        controller: AppController,
        runs: RunsRegistry,
        runs_vm: RunsVM,
        progress_vm: ProgressVM,
        settings_vm: SettingsVM,
        storage: StorageLocal,
        plate_vm: PlateVM,
        experiment_vm: ExperimentVM,
        runs_panel,
        ensure_adapter,
        toast_error,
        build_plan: BuildExperimentPlan,
        build_storage_meta: BuildStorageMeta,
    ) -> None:
        """Initialize presenter dependencies and local runtime state.

        Args:
            win: Root window used for status/toast updates.
            controller: App controller exposing adapters and use-cases.
            runs: Persistent runs registry.
            runs_vm: Viewmodel backing the runs tab.
            progress_vm: Viewmodel backing run overview/activity tabs.
            settings_vm: Settings viewmodel for polling/download preferences.
            storage: Storage adapter used for download/extraction workflows.
            plate_vm: Plate selection/configuration viewmodel.
            experiment_vm: Experiment form/state viewmodel.
            runs_panel: Runs panel view instance.
            ensure_adapter: Callback that ensures adapter wiring is ready.
            toast_error: Callback that formats exceptions into UI messages.
            build_plan: Use-case building a typed experiment plan.
            build_storage_meta: Use-case building storage metadata.
        """
        self._log = logging.getLogger(__name__)
        self.win = win
        self.controller = controller
        self.runs = runs
        self.runs_vm = runs_vm
        self.progress_vm = progress_vm
        self.settings_vm = settings_vm
        self.storage = storage
        self.plate_vm = plate_vm
        self.experiment_vm = experiment_vm
        self.runs_panel = runs_panel
        self._ensure_adapter = ensure_adapter
        self._toast_error = toast_error
        self._build_plan = build_plan
        self._build_storage_meta = build_storage_meta

        self._sessions: Dict[str, FlowSession] = {}
        self._active_group_id: Optional[str] = None
        self._group_storage_meta: Dict[str, StorageMeta] = {}
        self._last_download_dir: Optional[str] = None

        self._scheduler = PollingScheduler(win.after, win.after_cancel)
        self._activity_delay_ms: int = 2000
        self._activity_signature: Optional[Tuple[Tuple[str, str], ...]] = None
        # Tracks the last-known device connectivity outcome so the statusbar
        # message is only touched on an actual change (connected <->
        # disconnected), not clobbered by every poll tick -- it shares its
        # StringVar with toast messages.
        self._devices_reachable: Optional[bool] = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------
    @property
    def active_group_id(self) -> Optional[str]:
        """Return currently active run-group id, if any."""
        return self._active_group_id

    @property
    def last_download_dir(self) -> Optional[str]:
        """Return most recent downloaded results directory, if available."""
        return self._last_download_dir

    def group_storage_meta_for(self, group_id: str) -> Optional[StorageMeta]:
        """Return cached storage metadata for a run group.

        Args:
            group_id: Run-group identifier.
        """
        return self._group_storage_meta.get(group_id)

    def record_download_dir(self, path: str) -> None:
        """Remember the most recent download directory for quick-open actions.

        Args:
            path: Download directory path.
        """
        self._last_download_dir = path

    # ------------------------------------------------------------------
    # Registry wiring
    # ------------------------------------------------------------------
    def configure_runs_registry(self, store_path: Path) -> None:
        """Configure the runs registry and re-attach persisted groups.

        Args:
            store_path: JSON path used by ``RunsRegistry`` persistence.
        """
        self.runs.configure(
            store_path=store_path,
            hooks_factory=self._build_flow_hooks_for_group,
            coordinator_factory=self._coordinator_factory_for_group,
        )
        try:
            self.runs.load()
        except Exception as exc:
            self._log.warning("Failed to load runs registry: %s", exc)
            return

        for group_id in self.runs.active_groups():
            try:
                context = self.runs.start_tracking(group_id)
            except Exception as exc:
                self._log.error("Failed to re-attach group %s: %s", group_id, exc)
                continue
            coordinator = self.runs.coordinator_for(group_id)
            if not coordinator or not context:
                continue
            self._register_session(group_id, coordinator, context)
            self._schedule_poll(group_id, 0)
        self._refresh_runs_panel()

    def _build_flow_hooks_for_group(self, group_id: str) -> FlowHooks:
        """Create FlowHooks bound to a specific run group.

        Args:
            group_id: Group id used to bind callbacks.
        """
        return FlowHooks(
            on_started=lambda ctx: self._on_group_started(group_id, ctx),
            on_snapshot=lambda snapshot: self._on_group_snapshot(group_id, snapshot),
            on_completed=lambda path: self._on_group_completed(group_id, path),
            on_error=lambda message: self._on_group_error(group_id, message),
        )

    def _coordinator_factory_for_group(
        self,
        group_id: str,
        plan_meta: PlanMeta,
        storage_meta: StorageMeta,
        hooks: FlowHooks,
    ) -> RunFlowCoordinator:
        """Create a coordinator for persisted-group reattachment.

        Args:
            group_id: Group id being reattached.
            plan_meta: Persisted plan metadata for the group.
            storage_meta: Persisted storage metadata.
            hooks: Hook callbacks bound by the registry.
        """
        if not self._ensure_adapter():
            raise RuntimeError("Adapters not configured for coordinator factory.")
        coordinator = RunFlowCoordinator(
            job_port=self.controller.job_adapter,
            storage_port=self.storage,
            uc_start=self.controller.uc_start,
            uc_poll=self.controller.uc_poll,
            uc_download=self.controller.uc_download,
            settings=self.settings_vm,
            hooks=hooks,
        )
        return coordinator

    def _register_session(
        self, group_id: str, coordinator: RunFlowCoordinator, context: GroupContext
    ) -> None:
        """Register a runtime session both locally and in the registry.

        Args:
            group_id: Group id key.
            coordinator: Active coordinator for group lifecycle.
            context: Start/attach context associated with the group.
        """
        self.runs.register_runtime(group_id, coordinator, context)
        self._sessions[group_id] = FlowSession(coordinator=coordinator, context=context)
        self._group_storage_meta.setdefault(group_id, context.storage_meta)
        if not self._active_group_id:
            self._set_active_group(group_id)
        self._refresh_runs_panel()

    # ------------------------------------------------------------------
    # Runs panel helpers
    # ------------------------------------------------------------------
    def _refresh_runs_panel(self) -> None:
        """Push current registry rows and selection into the runs panel view."""
        if not self.runs_panel:
            return
        rows = self.runs_vm.rows()
        self.runs_panel.set_rows(rows)
        active = self.runs_vm.active_group_id or self._active_group_id
        current_vm = getattr(self.progress_vm, "active_group_id", None)
        if active and active != current_vm:
            self.runs_panel.select_group(active)

    def refresh_runs_panel(self) -> None:
        """Public wrapper used by app bootstrap and controllers."""
        self._refresh_runs_panel()

    def build_download_toast(self, group_id: str, path: str) -> str:
        """Public wrapper returning standardized download toast text.

        Args:
            group_id: Run-group id.
            path: Download output path.
        """
        return self._build_download_toast(group_id, path)

    def stop_all_polling(self) -> None:
        """Stop group polling and device-activity polling."""
        self._stop_polling()
        self._stop_activity_polling()

    def _open_path(self, path: str) -> None:
        """Open a folder path with platform-default file explorer.

        Args:
            path: Filesystem path to open.
        """
        if not path:
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.check_call(["open", path])
            else:
                subprocess.check_call(["xdg-open", path])
        except Exception:
            messagebox.showwarning("Open Folder", f"Could not open folder:\n{path}")

    def on_runs_select(self, group_id: str) -> None:
        """Handle user selection change in the runs panel.

        Args:
            group_id: Selected run-group id.
        """
        if self.progress_vm.active_group_id == group_id:
            self.runs_vm.set_active_group(group_id)
            self._active_group_id = group_id
            return

        self.runs_vm.set_active_group(group_id)
        self.progress_vm.set_active_group(group_id, self.runs)
        self._active_group_id = group_id

    def on_runs_open_folder(self, group_id: str) -> None:
        """Open downloaded results folder for a selected group if available.

        Args:
            group_id: Selected run-group id.
        """
        entry = self.runs.get(group_id)
        if not entry:
            return
        path = (entry.download.path or "").strip()
        if not path:
            messagebox.showinfo("Open Folder", "No download directory available yet.")
            return
        self._open_path(path)

    def on_runs_cancel(self, group_id: str) -> None:
        """Prompt then cancel a selected run group.

        Args:
            group_id: Selected run-group id.
        """
        if not self._ensure_adapter() or not self.controller.uc_cancel:
            messagebox.showinfo("Cancel Group", "Cancel use case not available.")
            return

        entry = self.runs.get(group_id)
        if not entry:
            messagebox.showinfo("Cancel Group", "Entry not found.")
            return
        if entry.status not in {"running", "pending"}:
            messagebox.showinfo("Cancel Group", "Run is no longer active.")
            return
        if not messagebox.askyesno("Cancel Group", f"Really cancel group {group_id}?"):
            return

        try:
            self.controller.uc_cancel(group_id)  # type: ignore[misc]
            self._stop_polling(group_id)
            self.runs.mark_cancelled(group_id)
            self._refresh_runs_panel()
        except Exception as exc:
            messagebox.showerror("Cancel Group", f"Cancel failed:\n{exc}")

    def on_runs_delete(self, group_id: str) -> None:
        """Remove a run entry, optionally canceling first if still active.

        Args:
            group_id: Selected run-group id.
        """
        entry = self.runs.get(group_id)
        if not entry:
            return

        if entry.status in {"running", "pending"}:
            if not messagebox.askyesno(
                "Cancel Group", f"Group {group_id} is still running. Cancel now?"
            ):
                return
            self.on_runs_cancel(group_id)
            return

        if not messagebox.askyesno("Remove", f"Remove entry {group_id} from the list?"):
            return

        self._stop_polling(group_id)
        self.runs.remove(group_id)
        if self.runs_vm.active_group_id == group_id:
            self.runs_vm.set_active_group(None)
            self.progress_vm.set_active_group(None, self.runs)
        if self._active_group_id == group_id:
            self._active_group_id = next(iter(self._sessions), None)
        self._refresh_runs_panel()

    # ------------------------------------------------------------------
    # Run flow actions
    # ------------------------------------------------------------------
    def start_run(self) -> None:
        """Handle toolbar submit triggered by the user."""
        try:
            if not self._ensure_adapter():
                return

            configured = self.plate_vm.configured()
            if not configured:
                self.win.show_toast("No configured wells to start.")
                return

            selection = self.plate_vm.get_selection()
            self.experiment_vm.set_selection(selection)
            plan_request = self._build_plan_request(configured)
            plan = self._build_plan(plan_request)
            storage_meta = self._build_storage_meta(plan.meta, self.settings_vm)
            boxes = sorted(
                {
                    box
                    for wid in configured
                    if isinstance(wid, str)
                    for box in [well_id_to_box(wid)]
                    if box is not None
                }
            )
            summary = {
                "wells": len(configured),
                "boxes": boxes or ["-"],
                "stream": bool(self.settings_vm.use_streaming),
            }
            self._log.info("Submitting start request: %s", summary)
            self._log.debug("Start selection=%s", sorted(configured))

            start_hooks = FlowHooks()
            coordinator = RunFlowCoordinator(
                job_port=self.controller.job_adapter,
                storage_port=self.storage,
                uc_start=self.controller.uc_start,
                uc_poll=self.controller.uc_poll,
                uc_download=self.controller.uc_download,
                settings=self.settings_vm,
                hooks=start_hooks,
            )

            try:
                ctx = coordinator.start(plan, storage_meta)
            except UseCaseError as exc:
                if getattr(exc, "code", "") == "SLOT_BUSY":
                    meta = getattr(exc, "meta", None) or {}
                    busy = []
                    if isinstance(meta, dict) and isinstance(meta.get("busy_wells"), list):
                        busy = [str(w) for w in meta.get("busy_wells")]
                    msg = exc.message or "Slots busy."
                    try:
                        self.win.show_toast(f"Start rejected: {msg}")
                    except Exception:
                        self.win.show_toast(f"Start rejected: {msg}")
                    if busy and hasattr(self.plate_vm, "flash_wells"):
                        try:
                            self.plate_vm.flash_wells(busy)
                        except Exception:
                            pass
                    coordinator.stop_polling()
                    return
                raise
            start_result = coordinator.last_start_result()
            if not isinstance(start_result, StartBatchResult):
                raise RuntimeError("Coordinator returned an unexpected start result.")

            if not start_result.run_group_id:
                coordinator.stop_polling()
                return

            group_id = str(ctx.group)
            subruns = start_result.per_box_runs
            meta = ctx.meta
            self._group_storage_meta[group_id] = storage_meta
            self._log.info(
                "Start response: group=%s wells=%d boxes=%s",
                group_id,
                len(plan.wells),
                sorted(subruns.keys()),
            )
            self._log.debug("Start run map: %s", subruns)

            self.runs.add(
                group_id=group_id,
                name=meta.experiment,
                boxes=sorted(subruns.keys()),
                runs_by_box=subruns,
                plan_meta=meta,
                storage_meta=storage_meta,
            )
            self._refresh_runs_panel()

            tracking_hooks = self._build_flow_hooks_for_group(group_id)
            coordinator.hooks = tracking_hooks
            self._register_session(group_id, coordinator, ctx)
            self.runs.start_tracking(group_id)

            self._set_active_group(group_id)

            started_boxes = ", ".join(sorted(subruns.keys()))
            if started_boxes:
                self.win.show_toast(f"Started group {group_id} on {started_boxes}")
            else:
                self.win.show_toast(f"Started group {group_id}.")
            self._schedule_poll(group_id, 0)
        except Exception as exc:
            self._stop_polling()
            self._toast_error(exc)

    def cancel_active_group(self) -> None:
        """Cancel the currently active run group through the cancel use-case."""
        if not self._active_group_id or not self._ensure_adapter():
            self.win.show_toast("No active group.")
            return
        try:
            current = self._active_group_id
            self._log.info("Cancel requested for group %s", current)
            self.controller.uc_cancel(current)  # type: ignore[misc]
            self._stop_polling(current)
            self.runs.mark_cancelled(current)
            self.win.show_toast(f"Cancel requested for group {current}.")
            self._refresh_runs_panel()
            self.progress_vm.set_active_group(current, self.runs)
        except Exception as exc:
            self._toast_error(exc)

    def cancel_selected_runs(self) -> None:
        """Cancel only runs that correspond to currently selected wells.

        Not currently called from the composition root (the "End Selection"
        button that triggered this was removed from the view); kept as
        public presenter API for a future UI entry point.
        """
        selection = sorted(self.plate_vm.get_selection())
        if not selection:
            self.win.show_toast("Select at least one well.")
            return
        if not self._ensure_adapter():
            return
        cancel_runs = self.controller.uc_cancel_runs
        if cancel_runs is None:
            self.win.show_toast("Cancel selected runs not available.")
            return

        payload = self.progress_vm.map_selection_to_runs(selection)
        if not payload:
            self.win.show_toast("Selected wells have no active runs.")
            return

        try:
            self._log.info(
                "End selection requested for wells: %s", ", ".join(selection)
            )
            request = {"box_runs": payload, "span": "selected"}
            cancel_runs(request)
            self.win.show_toast("Abort requested for selected runs.")
        except Exception as exc:
            self._toast_error(exc, context="Cancel runs")

    # ------------------------------------------------------------------
    # Polling helpers
    # ------------------------------------------------------------------
    def _stop_polling(self, group_id: Optional[str] = None) -> None:
        """Cancel scheduled polls and stop coordinators.

        Args:
            group_id: Optional single group id; when ``None`` all are stopped.
        """
        if group_id is None:
            for gid in list(self._sessions.keys()):
                self._stop_polling(gid)
            return

        session = self._sessions.get(group_id)
        if not session:
            return
        self._scheduler.cancel(group_id)
        try:
            session.coordinator.stop_polling()
        except Exception:
            pass
        self.runs.unregister_runtime(group_id)
        self._sessions.pop(group_id, None)
        if self._active_group_id == group_id:
            self._active_group_id = next(iter(self._sessions), None)

    def start_activity_polling(self) -> None:
        """Start adaptive device-activity polling across configured boxes."""
        self._activity_delay_ms = 2000
        self._activity_signature = None
        self._schedule_activity_poll(self._activity_delay_ms)

    def _stop_activity_polling(self) -> None:
        """Stop the adaptive activity polling channel."""
        self._scheduler.cancel("activity")

    def _schedule_activity_poll(self, delay_ms: int) -> None:
        """Schedule next device-activity poll tick.

        Args:
            delay_ms: Delay in milliseconds for next tick.
        """
        delay = max(1, int(delay_ms))
        self._scheduler.schedule("activity", delay, self._on_activity_poll_tick)

    def _on_activity_poll_tick(self) -> None:
        """Poll device activity and adapt next delay based on change signature."""
        if not self.controller.ensure_ready() or not self.controller.uc_poll_device_status:
            self._update_connectivity_status(False)
            self._activity_delay_ms = 10000
            self._schedule_activity_poll(self._activity_delay_ms)
            return

        boxes = [
            str(box)
            for box, url in (self.settings_vm.api_base_urls or {}).items()
            if isinstance(url, str) and url.strip()
        ]
        if not boxes:
            self._update_connectivity_status(False)
            self._activity_delay_ms = 10000
            self._schedule_activity_poll(self._activity_delay_ms)
            return

        try:
            snapshot = self.controller.uc_poll_device_status(sorted(boxes))
        except Exception:
            # A device-communication failure here (timeout, connection
            # refused, ...) shouldn't crash the scheduled poll loop -- surface
            # it as a connectivity status change and keep polling, same
            # backoff as the "nothing configured" case above.
            self._log.warning("Activity poll failed; devices unreachable.", exc_info=True)
            self._update_connectivity_status(False)
            self._activity_delay_ms = 10000
            self._schedule_activity_poll(self._activity_delay_ms)
            return

        self._update_connectivity_status(True)
        signature = tuple(sorted((entry.well_id, entry.status) for entry in snapshot.entries))
        if signature == self._activity_signature:
            self._activity_delay_ms = min(10000, self._activity_delay_ms + 2000)
        else:
            self._activity_delay_ms = 2000
            self._activity_signature = signature

        self.progress_vm.apply_device_activity(snapshot)
        self._schedule_activity_poll(self._activity_delay_ms)

    def _update_connectivity_status(self, reachable: bool) -> None:
        """Update the statusbar message, but only on an actual change in
        device-connectivity state -- it shares its StringVar with toast
        messages, so this must not overwrite those on every poll tick.

        Args:
            reachable: Whether the most recent device poll succeeded.
        """
        if reachable == self._devices_reachable:
            return
        self._devices_reachable = reachable
        self.win.set_status_message("Idle." if reachable else "No devices connected.")

    def _schedule_poll(self, group_id: str, delay_ms: int) -> None:
        """Schedule the next poll tick for a given group.

        Args:
            group_id: Group id whose polling should continue.
            delay_ms: Delay in milliseconds before next poll.
        """
        session = self._sessions.get(group_id)
        if not session:
            return
        delay = max(1, int(delay_ms))
        self._scheduler.schedule(group_id, delay, lambda gid=group_id: self._on_poll_tick(gid))

    def _on_poll_tick(self, group_id: str) -> None:
        """Cooperative poll tick executed on the Tkinter thread.

        Args:
            group_id: Group id currently being polled.
        """
        session = self._sessions.get(group_id)
        if not session:
            return
        if not self._ensure_adapter():
            return

        coordinator = session.coordinator
        context = session.context

        tick: FlowTick = coordinator.poll_once(context)
        if tick.event == "tick":
            delay = tick.next_delay_ms
            if delay is None:
                base = getattr(self.settings_vm, "poll_interval_ms", 1000) or 1000
                try:
                    delay = max(200, int(base))
                except (TypeError, ValueError):
                    delay = 1000
            self._schedule_poll(group_id, int(delay))
            return

        self._scheduler.cancel(group_id)
        if tick.event == "completed":
            coordinator.stop_polling()
            auto_download_enabled = getattr(
                self.settings_vm, "auto_download_on_complete", True
            )
            if tick.snapshot:
                try:
                    download_path = coordinator.on_completed(context, tick.snapshot)
                except Exception as exc:
                    self._toast_error(exc, context="Download failed")
                    self._finalize_session(group_id)
                    return
                if auto_download_enabled:
                    self._on_group_completed(group_id, download_path)
            if not auto_download_enabled:
                self._on_group_completed(group_id, None)
            self._finalize_session(group_id)
            return

        if tick.event == "error":
            coordinator.stop_polling()
            self._finalize_session(group_id)

    def _finalize_session(self, group_id: str) -> None:
        """Clean up local bookkeeping once a run no longer needs polling.

        Args:
            group_id: Group id whose session should be finalized.
        """
        self._sessions.pop(group_id, None)
        self._scheduler.cancel(group_id)
        self.runs.unregister_runtime(group_id)
        if self._active_group_id == group_id:
            self._active_group_id = next(iter(self._sessions), None)
        self._refresh_runs_panel()

    def _on_group_started(self, group_id: str, ctx: GroupContext) -> None:
        """Handle group-start event emitted by coordinator hooks.

        Args:
            group_id: Group id that started.
            ctx: Coordinator start context.
        """
        self._log.debug("Coordinator acknowledged start for group %s", ctx.group)

    def _on_group_snapshot(self, group_id: str, snapshot) -> None:
        """Handle snapshot event emitted by coordinator hooks.

        Args:
            group_id: Group id that produced the snapshot.
            snapshot: Latest group snapshot DTO/domain object.
        """
        self.runs.update_snapshot(group_id, snapshot)
        if snapshot and group_id == self._active_group_id:
            self.progress_vm.apply_snapshot(snapshot)
        self._refresh_runs_panel()

    def _on_group_completed(self, group_id: str, path: Optional[Path]) -> None:
        """Handle group-completion event emitted by coordinator hooks.

        Args:
            group_id: Completed group id.
            path: Optional download output path.
        """
        download_path = os.path.abspath(str(path)) if path else None
        self.runs.mark_done(group_id, download_path)
        if download_path:
            self._last_download_dir = download_path
            self.win.show_toast(self._build_download_toast(group_id, download_path))
        else:
            self.win.show_toast("All runs completed.")
        self._refresh_runs_panel()
        if getattr(self.progress_vm, "active_group_id", None) == group_id:
            self.progress_vm.set_active_group(group_id, self.runs)

    def _on_group_error(self, group_id: str, message: str) -> None:
        """Handle polling-error event emitted by coordinator hooks.

        Args:
            group_id: Group id that failed.
            message: Error message text from polling pipeline.
        """
        text = message.strip() if isinstance(message, str) else ""
        if text:
            self._log.error("Polling error for %s: %s", group_id, text)
        else:
            self._log.error("Polling error for %s: <empty message>")
        self.runs.mark_error(group_id, text or None)
        self.win.show_toast(text or "Polling failed.")
        self._refresh_runs_panel()

    # ------------------------------------------------------------------
    # Plan building
    # ------------------------------------------------------------------
    def _build_plan_request(self, configured) -> ExperimentPlanRequest:
        """Build typed plan request from current VM state.

        Args:
            configured: Iterable of configured well ids.
        """
        wells = tuple(str(well) for well in configured)
        experiment_name = getattr(self.settings_vm, "experiment_name", "") or ""
        subdir = getattr(self.settings_vm, "subdir", None)
        override = ""
        if hasattr(self.experiment_vm, "fields"):
            override = str(self.experiment_vm.fields.get("storage.client_datetime") or "")

        well_snapshots = []
        source_params = getattr(self.experiment_vm, "well_params", {}) or {}
        for well_id, modes in source_params.items():
            mode_snapshots = tuple(
                ModeSnapshot(name=str(mode), params=dict(params))
                for mode, params in modes.items()
            )
            well_snapshots.append(
                WellSnapshot(well_id=str(well_id), modes=mode_snapshots)
            )

        return ExperimentPlanRequest(
            experiment_name=experiment_name,
            subdir=subdir,
            client_datetime_override=override,
            wells=wells,
            well_snapshots=tuple(well_snapshots),
        )

    # ------------------------------------------------------------------
    # Download toast helpers
    # ------------------------------------------------------------------
    def _build_download_toast(self, group_id: str, path: str) -> str:
        """Build user-facing toast text for completed downloads.

        Args:
            group_id: Group id associated with the download.
            path: Filesystem download path.
        """
        short_path = self._shorten_download_path(path)
        descriptor = ""
        meta = self._group_storage_meta.get(group_id) if group_id else None
        if not meta and group_id:
            entry = self.runs.get(group_id)
            if entry:
                meta = entry.storage_meta
        if meta:
            parts = [
                meta.experiment.strip(),
                (meta.subdir or "").strip(),
                meta.client_datetime_label(),
            ]
            descriptor = "/".join([p for p in parts if p])
        target = f"{descriptor} -> {short_path}" if descriptor else short_path
        if self._can_open_results_folder():
            return f"Results unpacked to {target} (Ctrl+Shift+O to open)"
        return f"Results unpacked to {target}"

    @staticmethod
    def _shorten_download_path(path: str, max_len: int = 60) -> str:
        """Truncate long paths for compact toast rendering.

        Args:
            path: Path string to shorten.
            max_len: Maximum output length.
        """
        normalized = os.path.normpath(path)
        if len(normalized) <= max_len:
            return normalized
        suffix_len = max(3, max_len - 3)
        return f"...{normalized[-suffix_len:]}"

    @staticmethod
    def _can_open_results_folder() -> bool:
        """Return whether this runtime can open directories directly."""
        if sys.platform.startswith("win"):
            return hasattr(os, "startfile")
        if sys.platform == "darwin":
            return True
        return shutil.which("xdg-open") is not None

    # ------------------------------------------------------------------
    # Active group helpers
    # ------------------------------------------------------------------
    def _set_active_group(self, group_id: Optional[str]) -> None:
        """Synchronize active-group selection across win and viewmodels.

        Args:
            group_id: Group id to activate, or ``None``.
        """
        self._active_group_id = group_id
        self.runs_vm.set_active_group(group_id)
        self.progress_vm.set_active_group(group_id, self.runs)
        if self.runs_panel and group_id:
            self.runs_panel.select_group(group_id)
        self._refresh_runs_panel()


__all__ = ["FlowSession", "RunFlowPresenter"]
