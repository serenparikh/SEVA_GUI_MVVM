"""Cyclic voltammetry parameter mapping from UI snapshots to domain payloads.

`build_experiment_plan` and related orchestration code use `CVParams` to
normalize mode-specific fields before adapters build REST payload bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, MutableMapping

from seva.domain.params import ModeParams

_FLAG_KEYS = (
    "run_cv",
    "run_dc",
    "run_ac",
    "run_eis",
    "run_lsv",
    "run_cdl",
    "eval_cdl",
)


@dataclass(frozen=True)
class CVParams(ModeParams):
    """Normalized CV parameters derived from the experiment form."""

    start: Any
    vertex1: Any
    vertex2: Any
    end: Any
    scan_rate: Any
    cycles: Any
    sampling_rate_hz: Any = None

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> "CVParams":
        """Create a params object from the flat CV form snapshot."""

        data: Dict[str, Any] = dict(form or {})
        return cls(
            start=data.get("cv.start_v") or 0.0,
            vertex1=data.get("cv.vertex1_v"),
            vertex2=data.get("cv.vertex2_v"),
            end=data.get("cv.final_v"),
            scan_rate=data.get("cv.scan_rate_v_s"),
            cycles=data.get("cv.cycles"),
            sampling_rate_hz=data.get("cv.sampling_rate_hz"),
            flags=cls._extract_flags(data),
        )

    def to_payload(self) -> Dict[str, Any]:
        """Serialize the parameters into the REST API payload.

        ``sampling_interval`` (seconds) is included only when the form's
        sampling rate (Hz) parses to a positive number; the backend applies
        it as CV-only CSV-row averaging and leaves other modes untouched.
        """

        payload: Dict[str, Any] = {
            "start": self.start,
            "vertex1": self.vertex1,
            "vertex2": self.vertex2,
            "end": self.end,
            "scan_rate": self.scan_rate,
            "cycles": self.cycles,
        }
        interval_s = self._sampling_interval_seconds()
        if interval_s is not None:
            payload["sampling_interval"] = interval_s
        return payload

    def _sampling_interval_seconds(self) -> Any:
        """Convert the form's sampling rate (Hz) into a CSV sampling interval (s)."""
        try:
            hz = float(self.sampling_rate_hz)
        except (TypeError, ValueError):
            return None
        if hz <= 0:
            return None
        return 1.0 / hz

    @classmethod
    def _extract_flags(cls, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Collect mode flags from nested and flat form snapshots."""
        flags: Dict[str, Any] = {}
        nested = data.get("flags")
        if isinstance(nested, MutableMapping):
            for key, val in nested.items():
                if cls._is_flag_key(key):
                    flags[key] = val
        for key, val in data.items():
            if cls._is_flag_key(key):
                flags[key] = val
        return dict(flags)

    @staticmethod
    def _is_flag_key(key: str) -> bool:
        """Return whether a field key should be treated as a run flag."""
        return key in _FLAG_KEYS or key.startswith("run_")
