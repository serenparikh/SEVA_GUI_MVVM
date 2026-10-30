"""Domain value objects and aggregates shared across adapters, use cases, and VMs.

The domain layer uses these immutable wrappers to keep validation close to
contract boundaries. Use cases build these types from adapter payloads and
pass them upward without leaking raw JSON structures.
"""

from __future__ import annotations


from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class GroupId:
    """Normalized identifier that ties together plans and snapshots across the system."""

    value: str
    """Sanitized identifier string safe for persistence, URLs, and file-system paths."""

    def __post_init__(self) -> None:
        """Reject blank identifiers before they enter plans and registries."""
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("GroupId must be a non-empty string.")

    def __str__(self) -> str:
        """Return the raw identifier string for serialization."""
        return self.value


@dataclass(frozen=True)
class RunId:
    """Identifier exposed by the backend for an individual run."""

    value: str
    """Backend-provided identifier string for the run, preserved verbatim."""

    def __post_init__(self) -> None:
        """Validate backend run identifiers before snapshot usage."""
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("RunId must be a non-empty string.")

    def __str__(self) -> str:
        """Expose the run identifier as text."""
        return self.value


@dataclass(frozen=True)
class WellId:
    """Identifier for a plate well within a plan or snapshot."""

    value: str
    """Domain identifier string for a well, typically in A1 notation."""

    def __post_init__(self) -> None:
        """Validate that well ids are non-empty strings."""
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("WellId must be a non-empty string.")

    def __str__(self) -> str:
        """Return the canonical well id string."""
        return self.value


@dataclass(frozen=True)
class BoxId:
    """Identifier for a potentiostat box or device enclosure."""

    value: str
    """Stable identifier string used to address a specific box."""

    def __post_init__(self) -> None:
        """Validate that box ids are non-empty strings."""
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("BoxId must be a non-empty string.")

    def __str__(self) -> str:
        """Return the canonical box id string."""
        return self.value


@dataclass(frozen=True)
class ModeName:
    """Logical name of the electrochemical mode executed for a well."""

    value: str
    """Name of the mode as understood by both the UI and the backend."""

    def __post_init__(self) -> None:
        """Validate mode names used by mode registry lookups."""
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("ModeName must be a non-empty string.")

    def __str__(self) -> str:
        """Return the mode name string."""
        return self.value


@dataclass(frozen=True)
class ClientDateTime:
    """Client-side timestamp captured when a plan is created."""

    value: datetime
    """Timezone-aware timestamp representing when the client issued the plan."""

    def __post_init__(self) -> None:
        """Ensure client timestamps include timezone information."""
        if not isinstance(self.value, datetime):
            raise TypeError("ClientDateTime requires a datetime instance.")
        if self.value.tzinfo is None or self.value.tzinfo.utcoffset(self.value) is None:
            raise ValueError("ClientDateTime must be timezone-aware.")

    def __str__(self) -> str:
        """Format the timestamp as ISO-8601 text."""
        return self.value.isoformat()


@dataclass(frozen=True)
class ServerDateTime:
    """Timestamp returned by the backend or hardware controller."""

    value: datetime
    """Timezone-aware timestamp reported by the backend for status updates."""

    def __post_init__(self) -> None:
        """Ensure server timestamps include timezone information."""
        if not isinstance(self.value, datetime):
            raise TypeError("ServerDateTime requires a datetime instance.")
        if self.value.tzinfo is None or self.value.tzinfo.utcoffset(self.value) is None:
            raise ValueError("ServerDateTime must be timezone-aware.")

    def __str__(self) -> str:
        """Format the timestamp as ISO-8601 text."""
        return self.value.isoformat()


@dataclass(frozen=True)
class ProgressPct:
    """Percentage-based progress indicator confined to 0–100 inclusive."""

    value: float
    """Progress value in percent, clamped to the inclusive range [0.0, 100.0]."""

    def __post_init__(self) -> None:
        """Normalize progress values and enforce the valid range."""
        if not isinstance(self.value, (float, int)):
            raise TypeError("ProgressPct expects a numeric percent value.")
        numeric = float(self.value)
        if numeric < 0.0 or numeric > 100.0:
            raise ValueError("ProgressPct must be within the inclusive range [0, 100].")
        object.__setattr__(self, "value", numeric)

    def __float__(self) -> float:
        """Return progress as float for math operations."""
        return self.value

    def __str__(self) -> str:
        """Return progress as a user-facing percentage string."""
        return f"{self.value:.2f}%"


@dataclass(frozen=True)
class Seconds:
    """Amount of elapsed or remaining time expressed in whole seconds."""

    value: int
    """Non-negative number of seconds representing a duration measurement."""

    def __post_init__(self) -> None:
        """Normalize durations to non-negative integer seconds."""
        if not isinstance(self.value, (int, float)):
            raise TypeError("Seconds expects an integer number of seconds.")
        numeric = int(self.value)
        if numeric < 0:
            raise ValueError("Seconds cannot be negative.")
        object.__setattr__(self, "value", numeric)

    def __int__(self) -> int:
        """Return the normalized integer duration."""
        return self.value

    def __str__(self) -> str:
        """Render duration as compact `Xs` text."""
        return f"{self.value}s"


@dataclass(frozen=True)
class ModeParams:
    """Base container for electrochemistry mode configuration."""

    flags: Mapping[str, Any]
    """Mode toggle flags derived from the mode definition."""

    def __post_init__(self) -> None:
        """Copy flags into a plain dict for immutable snapshot semantics."""
        mapping = dict(self.flags or {})
        object.__setattr__(self, "flags", mapping)

    def get_enabled(self) -> Iterable[str]:
        """Return an iterable of flag names that are enabled."""

        return (name for name, enabled in self.flags.items() if enabled)

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> "ModeParams":
        """Construct a params object from a flat form snapshot."""

        raise NotImplementedError("ModeParams subclasses must implement from_form().")

    def to_payload(self) -> Dict[str, Any]:
        """Serialize the parameters into the REST API payload schema."""

        raise NotImplementedError("ModeParams subclasses must implement to_payload().")


@dataclass(frozen=True)
class PlanMeta:
    """Describes the plan-level metadata shared across all wells."""

    experiment: str
    """Human-readable experiment name supplied by the operator."""
    subdir: Optional[str]
    """Optional subdirectory that further scopes exported results."""
    client_dt: ClientDateTime
    """Client-side timestamp capturing when the plan was assembled."""
    group_id: GroupId
    """Deterministic group identifier allocated for this plan."""
    make_plot: Optional[bool] = False
    """Whether the backend should generate plots for each run."""
    tia_gain: Optional[int] = 0
    """Transimpedance amplifier gain code (pyBEEP GAIN table: 0="1K" .. 4="10M").
    Defaults to 0 ("1K"), the widest current range, so conventional CV currents
    don't saturate; previously defaulting to None left the device's gain
    unconfigured (whatever it was last set to) since pyBEEP only sends
    SET_TIA_GAIN when a value is given."""
    sampling_interval: Optional[float] = None
    """Optional sampling interval override supplied by the client."""

    def __post_init__(self) -> None:
        """Trim plan metadata fields before orchestration persists them."""
        experiment = self.experiment.strip()
        if not experiment:
            raise ValueError("PlanMeta.experiment must not be blank.")
        object.__setattr__(self, "experiment", experiment)

        if self.subdir is not None:
            cleaned = self.subdir.strip()
            object.__setattr__(self, "subdir", cleaned or None)


@dataclass(frozen=True)
class WellPlan:
    """Plan for an individual well including its mode and configuration."""
    well: WellId
    modes: List[ModeName]
    params_by_mode: Dict[ModeName, ModeParams]


@dataclass(frozen=True)
class ExperimentPlan:
    """Aggregate capturing the entire experiment plan before submission."""
    meta: PlanMeta
    """Shared metadata applied to all well plans in the batch."""
    wells: List[WellPlan]

    def __post_init__(self) -> None:
        """Validate that experiment plans contain at least one typed well plan."""
        if not self.wells:
            raise ValueError("ExperimentPlan.wells must contain at least one WellPlan.")
        if not all(isinstance(plan, WellPlan) for plan in self.wells):
            raise TypeError("ExperimentPlan.wells must only contain WellPlan instances.")

        # object.__setattr__(self, "make_plot", bool(self.make_plot))


@dataclass(frozen=True)
class RunStatus:
    """Lifecycle information for a single run reported by polling."""

    run_id: RunId
    """Identifier assigned to the run by the backend."""
    phase: str
    """Current lifecycle phase label reported by the backend (e.g. queued, running, done)."""
    progress: Optional[ProgressPct] = None
    """Latest known progress percentage if provided by the backend."""
    remaining_s: Optional[Seconds] = None
    """Estimated number of seconds left until completion, if supplied."""
    error: Optional[str] = None
    """User-facing error message when the run fails, else None."""
    current_mode: Optional[str] = None
    remaining_modes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Normalize status phase values for consistent UI rendering."""
        if not isinstance(self.phase, str) or not self.phase.strip():
            raise ValueError("RunStatus.phase must be a non-empty string.")
        object.__setattr__(self, "phase", self.phase.strip())


@dataclass(frozen=True)
class BoxSnapshot:
    """Snapshot describing aggregate progress for a single box."""

    box: BoxId
    """Identifier of the box the snapshot was generated for."""
    progress: Optional[ProgressPct] = None
    """Aggregated progress percentage across runs on this box, if present."""
    remaining_s: Optional[Seconds] = None
    """Estimated remaining time in seconds until all runs on this box finish."""


@dataclass(frozen=True)
class GroupSnapshot:
    """Combined snapshot of all runs and boxes belonging to a plan group."""

    group: GroupId
    """Group identifier that the snapshot refers to."""
    runs: Dict[WellId, RunStatus]
    """Per-well mapping that surfaces the latest run status for each scheduled well."""
    boxes: Dict[BoxId, BoxSnapshot]
    """Per-box mapping summarizing aggregate progress state for each involved box."""
    all_done: bool = False
    """Flag indicating whether every run in the group has finished successfully."""

    def __post_init__(self) -> None:
        """Validate typed run and box maps produced by snapshot normalization."""
        self._validate_mapping("runs", self.runs, WellId, RunStatus)
        self._validate_mapping("boxes", self.boxes, BoxId, BoxSnapshot)

    @staticmethod
    def _validate_mapping(
        attr_name: str,
        mapping: Mapping,
        expected_key: type,
        expected_value: type,
    ) -> None:
        """Enforce key/value type contracts for snapshot aggregate mappings."""
        if not isinstance(mapping, Mapping):
            raise TypeError(f"GroupSnapshot.{attr_name} must be a mapping.")
        for key, value in mapping.items():
            if not isinstance(key, expected_key):
                raise TypeError(
                    f"GroupSnapshot.{attr_name} keys must be {expected_key.__name__} instances."
                )
            if not isinstance(value, expected_value):
                raise TypeError(
                    f"GroupSnapshot.{attr_name} values must be {expected_value.__name__} instances."
                )


__all__ = [
    "BoxId",
    "BoxSnapshot",
    "ClientDateTime",
    "ExperimentPlan",
    "GroupId",
    "GroupSnapshot",
    "ModeName",
    "ModeParams",
    "PlanMeta",
    "ProgressPct",
    "RunId",
    "RunStatus",
    "Seconds",
    "ServerDateTime",
    "WellId",
    "WellPlan",
]
