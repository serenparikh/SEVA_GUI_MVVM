"""Coordinator orchestrating the run flow without UI concerns."""

from __future__ import annotations


from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, TYPE_CHECKING

from seva.domain.entities import (
    ExperimentPlan,
    GroupId,
    GroupSnapshot,
    PlanMeta,
    RunId,
    WellId,
)
from seva.domain.storage_meta import StorageMeta
from seva.domain.ports import JobPort, StoragePort

if TYPE_CHECKING:  # pragma: no cover - type checking helper
    from seva.domain.settings import SettingsConfig

GroupRunIndex = Dict[WellId, RunId]
_UNSET = object()


def _noop(*_: object, **__: object) -> None:
    """Default no-op callback used for hooks."""


@dataclass(frozen=True)
class FlowTick:
    """Single decision unit returned by polling to guide scheduling."""

    event: str
    """Tick classification, e.g. 'tick', 'completed', or 'error'."""
    snapshot: Optional[GroupSnapshot] = None
    """Latest snapshot observed for the group when available."""
    next_delay_ms: Optional[int] = None
    """Optional delay hint for the next poll invocation."""
    error_msg: Optional[str] = None
    """Error information propagated when the flow transitions to failure."""


@dataclass
class GroupContext:
    """Mutable run context shared between flow steps."""

    group: GroupId
    """Identifier of the active run group."""
    meta: PlanMeta
    """Plan metadata captured when the run was prepared."""
    storage_meta: StorageMeta
    """Plan-level storage metadata used for downloads."""
    run_index: GroupRunIndex = field(default_factory=dict)
    """Mapping from well identifiers to backend run identifiers."""


@dataclass
class FlowHooks:
    """Optional callbacks triggered on significant flow events."""

    on_started: Callable[[GroupContext], None] = _noop
    on_snapshot: Callable[[GroupSnapshot], None] = _noop
    on_completed: Callable[[Path], None] = _noop
    on_error: Callable[[str], None] = _noop

    def __post_init__(self) -> None:
        """Normalize optional callbacks to no-op functions.

        Returns:
            None.

        Side Effects:
            Replaces falsey callback values with ``_noop``.
        """
        self.on_started = self.on_started or _noop
        self.on_snapshot = self.on_snapshot or _noop
        self.on_completed = self.on_completed or _noop
        self.on_error = self.on_error or _noop


class RunFlowCoordinator:
    """Coordinates the multi-step run lifecycle in discrete, UI-agnostic phases."""

    def __init__(
        self,
        job_port: JobPort,
        storage_port: StoragePort,
        uc_start,
        uc_poll,
        uc_download,
        settings: "SettingsConfig",
        hooks: Optional[FlowHooks] = None,
    ) -> None:
        """
        Initialize the coordinator with its collaborators.

        The injected use-case callables are kept untyped to avoid premature coupling
        while the flow contract is being extracted.
        """
        self.job_port = job_port
        self.storage_port = storage_port
        self.uc_start = uc_start
        self.uc_poll = uc_poll
        self.uc_download = uc_download
        self.settings = settings
        self.hooks = hooks or FlowHooks()
        self._active = False
        self._last_start_result: Optional[object] = None
        self._current_delay_ms: int = self._baseline_poll_interval()
        self._last_snapshot_signature: object = _UNSET
        self._completed_download_targets: Dict[str, Path] = {}

    def prepare_plan(
        self, vm_state: ExperimentPlan
    ) -> ExperimentPlan:
        """
        Transform the view-model state into a domain plan.

        For the initial skeleton the plan is passed through unchanged.
        """
        return vm_state

    def start(
        self, plan: ExperimentPlan, storage_meta: StorageMeta
    ) -> GroupContext:
        """
        Start the experiment batch and allocate a fresh group context.

        Emits the `on_started` hook with the constructed context.
        """
        meta = self._resolve_meta(plan)
        start_result = self.uc_start(plan)
        group_identifier = meta.group_id
        start_group_value = getattr(start_result, "run_group_id", None)
        if start_group_value:
            group_identifier = GroupId(str(start_group_value))
        if group_identifier != meta.group_id:
            meta = replace(meta, group_id=group_identifier)
        context = GroupContext(
            group=group_identifier,
            meta=meta,
            run_index={},
            storage_meta=storage_meta,
        )
        self._last_start_result = start_result
        self._active = True
        self._current_delay_ms = self._baseline_poll_interval()
        self._last_snapshot_signature = _UNSET
        self._completed_download_targets.pop(str(group_identifier), None)
        self.hooks.on_started(context)
        return context

    def attach(
        self,
        *,
        group_id: str,
        plan_meta: PlanMeta,
        storage_meta: StorageMeta,
        hooks: Optional[FlowHooks] = None,
    ) -> GroupContext:
        """
        Rebuild coordinator state for an already running group.

        Returns a fresh GroupContext that callers can reuse for polling.
        """
        if hooks is not None:
            self.hooks = hooks

        meta = plan_meta
        group_identifier = meta.group_id
        if str(group_identifier) != str(group_id):
            group_identifier = GroupId(str(group_id))
            meta = replace(meta, group_id=group_identifier)
        context = GroupContext(
            group=group_identifier,
            meta=meta,
            run_index={},
            storage_meta=storage_meta,
        )
        self._last_start_result = None
        self._active = True
        self._current_delay_ms = self._baseline_poll_interval()
        self._last_snapshot_signature = _UNSET
        self._completed_download_targets.pop(str(group_identifier), None)
        return context

    def poll_once(self, ctx: GroupContext) -> FlowTick:
        """
        Poll the backend once for the given group context.

        Emits the `on_snapshot` hook with the latest snapshot.
        """
        if not self._active:
            return FlowTick(event="stopped")

        try:
            snapshot = self.uc_poll(str(ctx.group))
        except Exception as exc:  # pragma: no cover - defensive guard
            self._active = False
            message = str(exc) or exc.__class__.__name__
            self.hooks.on_error(message)
            return FlowTick(event="error", error_msg=message)

        self.hooks.on_snapshot(snapshot)
        progress_changed = self._update_progress_state(snapshot)
        if snapshot and snapshot.all_done:
            self._active = False
            return FlowTick(event="completed", snapshot=snapshot)

        # Back off polling while no progress is reported to avoid busy looping.
        delay = self._compute_next_delay(progress_changed)
        return FlowTick(event="tick", snapshot=snapshot, next_delay_ms=delay)

    def on_completed(self, ctx: GroupContext, snapshot: GroupSnapshot) -> Path:
        """
        Handle flow completion for the specified context.

        Performs the optional auto-download and surfaces the extraction path
        through the completion hook.
        """
        if not snapshot or not snapshot.all_done:
            raise ValueError("Cannot complete flow without a finished snapshot.")

        group_key = str(ctx.group)
        cached = self._completed_download_targets.get(group_key)
        if cached is not None:
            return cached

        storage_meta = ctx.storage_meta
        results_dir = storage_meta.results_dir
        auto_download = self._auto_download_enabled()

        if auto_download:
            try:
                download_root = self.uc_download(
                    group_key,
                    results_dir,
                    storage_meta,
                    cleanup="delete",
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                message = str(exc) or exc.__class__.__name__
                self.hooks.on_error(message)
                raise
            path_obj = Path(download_root).resolve()
            self._completed_download_targets[group_key] = path_obj
            self.hooks.on_completed(path_obj)
            return path_obj

        fallback = Path(results_dir).resolve()
        self._completed_download_targets[group_key] = fallback
        return fallback

    def stop_polling(self) -> None:
        """
        Stop any further polling activity initiated by this coordinator.

        Future wiring will check `_active` before scheduling follow-up polls.
        """
        self._active = False
        self._current_delay_ms = self._baseline_poll_interval()
        self._last_snapshot_signature = _UNSET

    def last_start_result(self) -> Optional[object]:
        """Return the most recent start result for UI consumption when needed."""
        return self._last_start_result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _baseline_poll_interval(self) -> int:
        """Return the configured baseline poll interval in milliseconds."""
        raw = getattr(self.settings, "poll_interval_ms", 1000)
        try:
            delay = int(raw)
        except (TypeError, ValueError):
            delay = 1000
        delay = max(200, delay)
        return delay

    def _max_poll_backoff(self, baseline: int) -> int:
        """Return the effective backoff ceiling, honoring configuration defaults."""
        raw = getattr(self.settings, "poll_backoff_max_ms", None)
        try:
            limit = int(raw) if raw is not None else 5000
        except (TypeError, ValueError):
            limit = 5000
        if limit <= 0:
            limit = 5000
        return max(baseline, limit)

    def _compute_next_delay(self, progress_changed: bool) -> int:
        """Adjust the poll interval based on recent progress observations."""
        baseline = self._baseline_poll_interval()
        max_delay = self._max_poll_backoff(baseline)
        if progress_changed:
            self._current_delay_ms = baseline
            return self._current_delay_ms

        current = self._current_delay_ms or baseline
        next_delay = int(max(current * 1.5, current + 1))
        self._current_delay_ms = min(next_delay, max_delay)
        return self._current_delay_ms

    def _update_progress_state(
        self, snapshot: Optional[GroupSnapshot]
    ) -> bool:
        """Record the latest snapshot signature and report whether it changed."""
        signature = self._snapshot_signature(snapshot)
        previous = self._last_snapshot_signature
        self._last_snapshot_signature = signature
        if previous is _UNSET:
            return True
        return signature != previous

    def _snapshot_signature(
        self, snapshot: Optional[GroupSnapshot]
    ) -> Optional[tuple]:
        """Build a compact descriptor capturing phases and progress for diffing."""
        if not snapshot:
            return None

        run_entries = []
        for well, status in sorted(
            snapshot.runs.items(), key=lambda item: str(item[0])
        ):
            progress = (
                round(status.progress.value, 3)
                if getattr(status, "progress", None) is not None
                else None
            )
            remaining = (
                int(status.remaining_s.value)
                if getattr(status, "remaining_s", None) is not None
                else None
            )
            error = (status.error or "").strip() or None
            run_entries.append(
                (
                    str(status.run_id),
                    status.phase,
                    progress,
                    remaining,
                    error,
                )
            )

        box_entries = []
        for box, box_status in sorted(
            snapshot.boxes.items(), key=lambda item: str(item[0])
        ):
            progress = (
                round(box_status.progress.value, 3)
                if getattr(box_status, "progress", None) is not None
                else None
            )
            remaining = (
                int(box_status.remaining_s.value)
                if getattr(box_status, "remaining_s", None) is not None
                else None
            )
            box_entries.append((str(box), progress, remaining))

        return (
            snapshot.all_done,
            tuple(run_entries),
            tuple(box_entries),
        )

    def _auto_download_enabled(self) -> bool:
        """Interpret the auto-download toggle with a sensible default."""
        value = getattr(self.settings, "auto_download_on_complete", True)
        if isinstance(value, bool):
            return value
        if value is None:
            return True
        return bool(value)

    def _resolve_meta(self, plan: ExperimentPlan) -> PlanMeta:
        """Extract plan metadata from the domain object."""
        if isinstance(plan, ExperimentPlan):
            return plan.meta
        raise TypeError("RunFlowCoordinator requires an ExperimentPlan instance.")

__all__ = [
    "FlowHooks",
    "FlowTick",
    "GroupContext",
    "RunFlowCoordinator",
]
