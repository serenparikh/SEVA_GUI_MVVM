"""REST adapter implementing ``JobPort`` run lifecycle operations.

This adapter maps typed ``ExperimentPlan`` input into backend job requests,
stores run-group bookkeeping needed for polling/cancel/download, and returns
server-authoritative snapshots.

Dependencies:
    - ``RetryingSession``/``HttpConfig`` for shared HTTP behavior.
    - ``api_errors`` helpers for typed non-2xx handling.
    - Domain mapping helpers for well/slot conversion.

Call context:
    - ``StartExperimentBatch`` calls ``start_batch``.
    - ``PollGroupStatus`` calls ``poll_group``.
    - ``CancelGroup`` and ``CancelRuns`` call cancel methods.
    - ``DownloadGroupResults`` calls ``download_group_zip``.
"""

# seva/adapters/job_rest.py
from __future__ import annotations

import logging
import os
from datetime import timezone
from typing import Dict, Iterable, Tuple, Optional, Any, List, Set
from uuid import uuid4

import requests

# Domain Port
from seva.domain.entities import ExperimentPlan
from seva.domain.util import normalize_mode_name
from seva.domain.mapping import (
    build_slot_registry,
    extract_device_entries,
    extract_slot_labels,
)
from seva.domain.ports import JobPort, RunGroupId, BoxId

from seva.adapters.http_client import HttpConfig, RetryingSession
from seva.adapters.api_errors import (
    ApiClientError,
    ApiError,
    ApiServerError,
    build_error_message,
    extract_error_code,
    extract_error_hint,
    parse_error_payload,
)


class JobRestAdapter(JobPort):
    """Run-lifecycle transport adapter for SEVA box APIs.

    Endpoints:
      - POST {base}/jobs               body: {"devices":["slot01"], "modes":["CV"], "params_by_mode":{...}}
              -> {"run_id": "..."}
      - POST {base}/jobs/status        -> [{"run_id":"...", "status":"running", ...}]
      - GET  {base}/runs/{run_id}/zip  -> application/zip

    Notes:
      - Cancel: POST /jobs/{run_id}/cancel per run.
      - Box list is dynamic from base_urls keys (alphabetic order).
      - Well/slot mapping uses a prebuilt registry (no ad-hoc arithmetic in call sites).
    """

    def __init__(
        self,
        base_urls: Dict[BoxId, str],
        api_keys: Optional[Dict[BoxId, str]] = None,
        request_timeout_s: int = 10,
        download_timeout_s: int = 60,
        retries: int = 2,
    ) -> None:
        """Initialize adapter and precompute well/slot registry.

        Args:
            base_urls: Mapping from ``BoxId`` to base URL.
            api_keys: Optional mapping from ``BoxId`` to API key.
            request_timeout_s: Timeout in seconds for API requests.
            download_timeout_s: Timeout in seconds for ZIP downloads.
            retries: Retry count for transport failures.

        Side Effects:
            Builds HTTP sessions and probes ``/devices`` to build slot registry.
        """
        self._log = logging.getLogger(__name__)
        self.base_urls = dict(base_urls)
        self.api_keys = dict(api_keys or {})
        self.cfg = HttpConfig(
            request_timeout_s=request_timeout_s,
            download_timeout_s=download_timeout_s,
            retries=retries,
        )
        # Dynamic, alphabetic box order
        self.box_order: List[BoxId] = sorted(self.base_urls.keys())

        # Sessions per box
        self.sessions: Dict[BoxId, RetryingSession] = {
            b: RetryingSession(self.api_keys.get(b), self.cfg)
            for b in self.base_urls.keys()
        }

        # Client-side group mapping: group_id -> {box -> run_id}
        self._groups: Dict[RunGroupId, Dict[BoxId, str]] = {}

        # Precompute registry well_id <-> (box, slot) once, then reuse in all
        # request mapping and download normalization paths.
        self.well_to_slot: Dict[str, Tuple[BoxId, int]] = {}
        self.slot_to_well: Dict[Tuple[BoxId, int], str] = {}
        self._build_registry()

        # Cached run snapshots + terminal tracking
        self._run_cache: Dict[str, Dict[str, Any]] = {}
        self._terminal_runs: Set[str] = set()

    # ---------- Registry ----------

    def _build_registry(self) -> None:
        """Build well/slot registry from server-reported device slots.

        Raises:
            ValueError: If any configured box reports zero available slots.

        Side Effects:
            Calls ``/devices`` on each configured box and fills ``well_to_slot``
            and ``slot_to_well`` mapping tables.
        """
        slots_by_box: Dict[BoxId, List[str]] = {}
        for box in self.box_order:
            payload = self._fetch_devices_payload(box)
            slots = extract_slot_labels(payload)
            if not slots:
                raise ValueError(f"No slots available for box '{box}'.")
            slots_by_box[box] = slots

        well_to_slot, slot_to_well = build_slot_registry(
            self.box_order, slots_by_box
        )
        self.well_to_slot = well_to_slot
        self.slot_to_well = slot_to_well

    # ---------- JobPort implementation ----------

    def health(self, box_id: BoxId) -> Dict:
        """Read ``/health`` payload for one box.

        Args:
            box_id: Box identifier to query.

        Returns:
            Health payload dictionary.

        Raises:
            ValueError: If no session/base URL is configured for the box.
            ApiError: For non-2xx HTTP responses.
            RuntimeError: If response payload is not a JSON object.
        """
        session = self.sessions.get(box_id)
        if session is None:
            raise ValueError(f"No session configured for box '{box_id}'")
        url = self._make_url(box_id, "/health")
        resp = session.get(url, timeout=self.cfg.request_timeout_s)
        self._ensure_ok(resp, f"health[{box_id}]")
        data = self._json_any(resp)
        if not isinstance(data, dict):
            raise RuntimeError(f"health[{box_id}]: expected dict response")
        return data

    def list_devices(self, box_id: BoxId) -> List[Dict]:
        """Read and normalize ``/devices`` payload.

        Args:
            box_id: Box identifier to query.

        Returns:
            List of device dictionaries normalized by domain mapping helpers.
        """
        payload = self._fetch_devices_payload(box_id)
        cleaned: List[Dict[str, Any]] = []
        for item in extract_device_entries(payload):
            cleaned.append(dict(item))
        return cleaned

    def start_batch(self, plan: ExperimentPlan) -> Tuple[RunGroupId, Dict[BoxId, List[str]]]:
        """Submit one backend job per planned well and collect run IDs.

        Args:
            plan: Typed experiment plan produced by plan-building use case.

        Returns:
            Tuple of ``(run_group_id, runs_by_box)``.

        Raises:
            TypeError: If ``plan`` is not an ``ExperimentPlan``.
            ValueError: If plan metadata/wells are invalid for submission.
            ApiError: If transport/session/response constraints fail.

        Side Effects:
            Performs ``POST /jobs`` calls, mutates in-memory group/run caches.

        Call Chain:
            ``StartExperimentBatch`` -> ``JobRestAdapter.start_batch``.
        """
        if not isinstance(plan, ExperimentPlan):
            raise TypeError(f"start_batch expects ExperimentPlan, got {type(plan).__name__}")

        meta = plan.meta
        client_dt = (
            meta.client_dt.value.astimezone(timezone.utc)
            .replace(microsecond=0)
        )
        client_dt_text = client_dt.isoformat()
        if client_dt_text.endswith("+00:00"):
            client_dt_text = client_dt_text[:-6] + "Z"

        group_id = str(meta.group_id) if str(meta.group_id).strip() else str(uuid4())
        experiment_name = (meta.experiment or "").strip()
        if not experiment_name:
            raise ValueError("start_batch: missing experiment_name in plan meta")
        subdir = (meta.subdir or "").strip() or None

        run_ids: Dict[BoxId, List[str]] = {}
        self._groups[group_id] = {}

        if not plan.wells:
            raise ValueError("start_batch: plan contains no wells")

        for well_plan in plan.wells:
            # Normalize mode tokens early so payloads are stable across UI aliases.
            normalized_modes = [
                normalized for mode in (well_plan.modes or [])
                if (normalized := normalize_mode_name(mode))
            ]
            well_id = str(well_plan.well)
            if not well_id:
                raise ValueError("Experiment plan contains an empty well identifier.")

            slot_info = self.well_to_slot.get(well_id)
            if not slot_info:
                raise ValueError(f"Unknown well '{well_id}' for any configured box.")
            box, slot = slot_info
            devices = [self._slot_label(slot)]

            params_by_mode: Dict[str, Dict[str, Any]] = {}
            for mode_name, params_obj in (well_plan.params_by_mode or {}).items():
                mode_key = str(mode_name)
                try:
                    # Keep serialization at adapter boundary: use cases provide
                    # typed params and adapter emits transport payload dicts.
                    params_by_mode[mode_key] = params_obj.to_payload()
                except NotImplementedError as exc:  # pragma: no cover
                    raise ValueError(
                        f"Well '{well_id}' mode '{mode_name}' parameters do not support payload serialization."
                    ) from exc
                except AttributeError as exc:  # pragma: no cover
                    raise ValueError(
                        f"Well '{well_id}' mode '{mode_name}' parameters are missing a to_payload() method."
                    ) from exc

            payload = {
                "devices": devices,
                "modes": normalized_modes,
                "params_by_mode": params_by_mode,
                "tia_gain": meta.tia_gain,
                "sampling_interval": meta.sampling_interval,
                "make_plot": bool(meta.make_plot),
                "experiment_name": experiment_name,
                "subdir": subdir,
                "group_id": group_id,
                "client_datetime": client_dt_text,
            }

            url = self._make_url(box, "/jobs")
            if self._log.isEnabledFor(logging.DEBUG):
                self._log.debug(
                    "POST start[%s]: well=%s devices=%s modes=%s group=%s",
                    box, well_id, devices, payload["modes"], group_id,
                )
            session = self.sessions.get(box)
            if session is None:
                raise ApiError(f"No HTTP session configured for box '{box}'", context=f"start[{box}]")
            resp = session.post(url, json_body=payload, timeout=self.cfg.request_timeout_s)

            self._ensure_ok(resp, f"start[{box}]")
            data = self._json(resp)
            run_id_raw = data.get("run_id")
            if not run_id_raw:
                raise ApiError("Response payload missing run_id", context=f"start[{box}]", payload=data)
            run_id = str(run_id_raw)

            self._groups[group_id].setdefault(box, []).append(run_id)
            run_ids.setdefault(box, []).append(run_id)

            normalized = self._normalize_job_status(box, data)
            self._store_run_snapshot(normalized)

        return group_id, run_ids

    def cancel_run(self, box_id: BoxId, run_id: str) -> None:
        """Cancel a single run.

        Args:
            box_id: Box identifier owning the run.
            run_id: Run identifier.

        Raises:
            ApiError: If session missing or backend returns non-2xx response.
        """
        session = self.sessions.get(box_id)
        if session is None:
            raise ApiError(
                f"No session configured for box '{box_id}'",
                context=f"cancel[{box_id}:{run_id}]",
            )
        self._cancel_run_with_session(session, box_id, run_id, ignore_missing=False)

    def cancel_runs(self, box_to_run_ids: Dict[BoxId, List[str]]) -> None:
        """Cancel multiple runs grouped by box ID.

        Args:
            box_to_run_ids: Mapping of ``box_id`` to run-id list.

        Raises:
            ApiError: If session missing or any backend cancel call fails.
        """
        if not box_to_run_ids:
            return
        for box, run_ids in box_to_run_ids.items():
            if not run_ids:
                continue
            session = self.sessions.get(box)
            if session is None:
                raise ApiError(
                    f"No session configured for box '{box}'",
                    context=f"cancel[{box}:*]",
                )
            seen: Set[str] = set()
            for run_id in run_ids:
                # Deduplicate per-box run IDs to avoid duplicate cancel calls.
                run_id_str = str(run_id or "").strip()
                if not run_id_str or run_id_str in seen:
                    continue
                seen.add(run_id_str)
                self._cancel_run_with_session(
                    session, box, run_id_str, ignore_missing=False
                )

    def cancel_group(self, run_group_id: RunGroupId) -> None:
        """Cancel all runs known for a run group.

        Args:
            run_group_id: Group identifier to cancel.

        Side Effects:
            Issues per-run cancel requests for all tracked runs in the group.

        Notes:
            Missing sessions are logged and skipped to preserve best-effort group
            cancellation behavior.
        """
        self._log.info("Cancel group %s requested.", run_group_id)
        box_runs = self._groups.get(run_group_id, {})
        if not box_runs:
            return
        for box, runs in box_runs.items():
            session = self.sessions.get(box)
            if session is None:
                self._log.warning(
                    "Cancel group %s: no session configured for box %s",
                    run_group_id,
                    box,
                )
                continue
            for run_id in runs:
                self._cancel_run_with_session(session, box, run_id, ignore_missing=True)

    def _cancel_run_with_session(
        self,
        session: RetryingSession,
        box: BoxId,
        run_id: str,
        *,
        ignore_missing: bool,
    ) -> None:
        """Cancel one run using a provided HTTP session.

        Args:
            session: Pre-resolved session for the target box.
            box: Box identifier.
            run_id: Run identifier.
            ignore_missing: If ``True``, 404 responses are treated as success.

        Raises:
            ApiError: On transport failure or non-allowed HTTP status.
        """
        url = self._make_url(box, f"/jobs/{run_id}/cancel")
        try:
            resp = session.post(url, timeout=self.cfg.request_timeout_s)
        except Exception as exc:
            raise ApiError(str(exc), context=f"cancel[{box}:{run_id}]") from exc
        if resp.status_code == 404 and ignore_missing:
            self._log.info(
                "Cancel run %s: already gone on box %s.", run_id, box
            )
            return
        self._ensure_ok(resp, f"cancel[{box}:{run_id}]")

    def poll_group(self, run_group_id: RunGroupId) -> Dict:
        """Poll all runs in a group and emit UI-compatible snapshot payload.

        Args:
            run_group_id: Group identifier to poll.

        Returns:
            Snapshot dictionary with ``boxes``, ``wells``, ``activity``, and
            ``all_done`` fields consumed by ``PollGroupStatus`` normalization.

        Raises:
            ApiError: On HTTP/session failures.
            RuntimeError: On invalid response payload shapes.

        Side Effects:
            Updates run cache and terminal-run tracking sets.
        """
        box_runs: Dict[BoxId, List[str]] = self._groups.get(run_group_id, {}) or {}
        if not box_runs:
            recovered = self._recover_group_runs(run_group_id)
            if recovered:
                self._groups[run_group_id] = recovered
                box_runs = recovered
        snapshot = {"boxes": {}, "wells": [], "activity": {}}

        has_runs = False
        all_terminal = True

        for box, run_list in box_runs.items():
            unique_runs: List[str] = list(dict.fromkeys(run_list))
            if not unique_runs:
                snapshot["boxes"][box] = {"runs": [], "phase": "Queued", "subrun": None}
                all_terminal = False
                continue

            pending_ids: List[str] = []
            for run_id in unique_runs:
                if run_id not in self._terminal_runs:
                    pending_ids.append(run_id)

            if pending_ids:
                self._log.debug(
                    "Poll group %s: box=%s pending_ids=%d",
                    run_group_id,
                    box,
                    len(pending_ids),
                )
                url = self._make_url(box, "/jobs/status")
                resp = self.sessions[box].post(
                    url,
                    json_body={"run_ids": pending_ids},
                    timeout=self.cfg.request_timeout_s,
                )
                self._ensure_ok(resp, f"status[{box}]")
                payload = self._json_any(resp)
                if not isinstance(payload, list):
                    raise RuntimeError("Invalid JSON response: expected list of runs")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    normalized = self._normalize_job_status(box, item)
                    self._store_run_snapshot(normalized)

            run_entries: List[Dict[str, Any]] = []
            statuses_capitalized: Set[str] = set()
            box_has_incomplete = False

            for run_id in unique_runs:
                data = self._run_cache.get(run_id)
                if not data:
                    data = {
                        "box": box,
                        "run_id": run_id,
                        "status": "queued",
                        "started_at": None,
                        "ended_at": None,
                        "progress_pct": 0,
                        "remaining_s": None,
                        "slots": [],
                    }
                    self._run_cache[run_id] = data
                    self._terminal_runs.discard(run_id)

                status = str(data.get("status") or "queued").lower()
                run_entry = {
                    "run_id": data.get("run_id", run_id),
                    "status": status,
                    "started_at": data.get("started_at"),
                    "ended_at": data.get("ended_at"),
                    "progress_pct": data.get("progress_pct", 0),
                    "remaining_s": data.get("remaining_s"),
                    "current_mode": data.get("current_mode") or data.get("mode"),
                    "remaining_modes": data.get("remaining_modes") or [],
                }
                run_entries.append(run_entry)
                statuses_capitalized.add(run_entry["status"].capitalize())

                if not self._is_terminal(status):
                    box_has_incomplete = True

                for slot_info in data.get("slots") or []:
                    slot_label = slot_info.get("slot")
                    try:
                        slot_num = int(str(slot_label).replace("slot", ""))
                    except Exception:
                        continue
                    wid = self.slot_to_well.get((box, slot_num))
                    if not wid:
                        continue
                    raw = str(slot_info.get("status") or "queued").lower()
                    s_status = (
                        "Error"
                        if raw == "failed"
                        else (
                            "Done"
                            if raw == "done"
                            else ("Running" if raw == "running" else "Queued")
                        )
                    )
                    s_msg = slot_info.get("message") or ""
                    snapshot["wells"].append(
                        {
                            "well": wid,
                            # Keep explicit UI labels here; use case normalizer
                            # converts this transport snapshot to domain objects.
                            "phase": s_status,  # Queued/Running/Done/Error
                            "progress_pct": data.get("progress_pct", 0),
                            "remaining_s": data.get("remaining_s"),
                            "error": s_msg,
                            "run_id": run_entry["run_id"],
                            # Include mode context so UI can render active/next.
                            "current_mode": data.get("current_mode") or data.get("mode"),
                            "remaining_modes": data.get("remaining_modes") or [],
                        }
                    )
                    snapshot["activity"][wid] = s_status

            if run_entries:
                has_runs = True
            else:
                all_terminal = False

            if not run_entries:
                box_phase = "Queued"
            elif len(statuses_capitalized) == 1:
                box_phase = next(iter(statuses_capitalized))
            else:
                box_phase = "Mixed"

            snapshot["boxes"][box] = {
                "runs": run_entries,
                "phase": box_phase,
                "subrun": ", ".join(entry["run_id"] for entry in run_entries)
                if run_entries
                else None,
            }

            if box_has_incomplete:
                all_terminal = False

        snapshot["all_done"] = bool(box_runs) and has_runs and all_terminal
        return snapshot

    def _recover_group_runs(self, run_group_id: RunGroupId) -> Dict[BoxId, List[str]]:
        """Recover run IDs for a group by querying ``/jobs?group_id=...``.

        Args:
            run_group_id: Group identifier to recover.

        Returns:
            Mapping of box IDs to recovered run IDs.

        Side Effects:
            Performs network requests per configured box.
        """
        recovered: Dict[BoxId, List[str]] = {}
        group_text = str(run_group_id or "").strip()
        if not group_text:
            return recovered
        for box in self.box_order:
            session = self.sessions.get(box)
            if session is None:
                continue
            url = self._make_url(box, f"/jobs?group_id={group_text}")
            resp = session.get(url, timeout=self.cfg.request_timeout_s)
            self._ensure_ok(resp, f"jobs[{box}]")
            payload = self._json_any(resp)
            if not isinstance(payload, list):
                raise RuntimeError("Invalid JSON response: expected list of jobs")
            run_ids: List[str] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                run_id_raw = item.get("run_id")
                if not run_id_raw:
                    continue
                run_id = str(run_id_raw)
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
            if run_ids:
                recovered[box] = run_ids
        if recovered and self._log.isEnabledFor(logging.DEBUG):
            self._log.debug("Recovered group %s runs=%s", run_group_id, recovered)
        return recovered

    def download_group_zip(self, run_group_id: RunGroupId, target_dir: str) -> str:
        """Download all run ZIP artifacts for a run group.

        Args:
            run_group_id: Group identifier to download.
            target_dir: Root directory where group folder should be created.

        Returns:
            Output path ``<target_dir>/<group_id>``.

        Side Effects:
            Creates directories and writes ZIP files to disk.
        """
        box_runs: Dict[BoxId, List[str]] = self._groups.get(run_group_id, {}) or {}
        out_dir = os.path.join(target_dir, str(run_group_id))
        os.makedirs(out_dir, exist_ok=True)

        for box, runs in box_runs.items():
            box_dir = os.path.join(out_dir, box)
            os.makedirs(box_dir, exist_ok=True)
            for run_id in runs:
                url = self._make_url(box, f"/runs/{run_id}/zip")
                resp = self.sessions[box].get(
                    url,
                    timeout=self.cfg.download_timeout_s,
                    stream=True,
                    accept="application/zip",
                )
                if resp.status_code == 404:
                    # Some runs may have been cleaned up server-side; skip quietly.
                    continue
                self._ensure_ok(resp, f"download[{box}:{run_id}]")

                path = os.path.join(box_dir, f"{run_id}.zip")
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        # Stream in chunks to avoid loading large archives in RAM.
                        if chunk:
                            f.write(chunk)

        return out_dir

    # ---------- helpers ----------

    def _make_url(self, box: BoxId, path: str) -> str:
        """Build endpoint URL from configured base URL.

        Args:
            box: Box identifier.
            path: Endpoint path beginning with ``/``.

        Returns:
            Absolute endpoint URL.

        Raises:
            ValueError: If box base URL is missing.
        """
        base = self.base_urls.get(box)
        if not base:
            raise ValueError(f"No base URL configured for box '{box}'")
        if base.endswith("/"):
            base = base[:-1]
        return f"{base}{path}"

    def _fetch_devices_payload(self, box_id: BoxId) -> Any:
        """Fetch raw ``/devices`` payload for registry discovery.

        Args:
            box_id: Box identifier to query.

        Returns:
            Parsed JSON payload (list/object) returned by backend.
        """
        session = self.sessions.get(box_id)
        if session is None:
            raise ValueError(f"No session configured for box '{box_id}'")
        url = self._make_url(box_id, "/devices")
        resp = session.get(url, timeout=self.cfg.request_timeout_s)
        self._ensure_ok(resp, f"devices[{box_id}]")
        return self._json_any(resp)

    def _slot_label(self, slot: int) -> str:
        """Format integer slot index as API slot token.

        Args:
            slot: Numeric slot index.

        Returns:
            Zero-padded slot token such as ``slot01``.
        """
        return f"slot{slot:02d}"

    def describe_well(self, well_id: str) -> Optional[str]:
        """Return a human-readable "Box <box> <slot>" label for a well.

        Args:
            well_id: Grid well identifier (e.g. "A2").

        Returns:
            Formatted label, or ``None`` when the well has no known device
            mapping (e.g. the registry predates that device's discovery).
        """
        slot_info = self.well_to_slot.get(well_id)
        if not slot_info:
            return None
        box, slot = slot_info
        return f"Box {box} {self._slot_label(slot)}"

    def _store_run_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Store run snapshot and update terminal tracking.

        Args:
            snapshot: Normalized run snapshot dictionary.
        """
        run_id = snapshot.get("run_id")
        if not run_id:
            return
        self._run_cache[run_id] = snapshot
        status = str(snapshot.get("status") or "").lower()
        if self._is_terminal(status):
            self._terminal_runs.add(run_id)
        else:
            self._terminal_runs.discard(run_id)

    @staticmethod
    def _is_terminal(status: str) -> bool:
        """Check whether status token represents terminal completion.

        Args:
            status: Run status token.

        Returns:
            ``True`` for terminal statuses, else ``False``.
        """
        normalized = status.lower()
        return normalized in {"done", "failed", "canceled", "cancelled"}

    def _normalize_job_status(
        self, box: BoxId, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Normalize backend run payload into adapter snapshot schema.

        Args:
            box: Box identifier associated with payload.
            payload: Raw JSON object from backend.

        Returns:
            Normalized snapshot dictionary with stable keys.

        Raises:
            RuntimeError: If payload shape is invalid.
        """
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid job status payload: expected object")
        run_id_raw = payload.get("run_id")
        if not run_id_raw:
            raise RuntimeError("Job status payload missing run_id")
        run_id = str(run_id_raw)
        status = str(payload.get("status") or "queued").lower() or "queued"
        slots = payload.get("slots") or []
        current_mode = payload.get("current_mode") or payload.get("mode")
        normalized = {
            "box": box,
            "run_id": run_id,
            "status": status,
            "started_at": payload.get("started_at"),
            "ended_at": payload.get("ended_at"),
            "progress_pct": payload.get("progress_pct") or 0,
            "remaining_s": payload.get("remaining_s"),
            "slots": slots,
            "mode": current_mode,
            "current_mode": current_mode,
            "modes": payload.get("modes") or [],
            "remaining_modes": payload.get("remaining_modes") or [],
        }
        return normalized

    @staticmethod
    def _ensure_ok(resp: requests.Response, ctx: str) -> None:
        """Raise typed adapter errors for non-2xx responses.

        Args:
            resp: HTTP response.
            ctx: Context label for diagnostics.

        Raises:
            ApiClientError: For HTTP 4xx responses.
            ApiServerError: For HTTP 5xx responses.
            ApiError: For all other non-2xx responses.
        """
        if 200 <= resp.status_code < 300:
            return
        status = resp.status_code
        payload = parse_error_payload(resp)
        message = build_error_message(ctx, status, payload)
        code = extract_error_code(payload)
        hint = extract_error_hint(payload)
        if 400 <= status < 500:
            raise ApiClientError(
                message,
                status=status,
                code=code,
                hint=hint,
                payload=payload,
                context=ctx,
            )
        if 500 <= status < 600:
            raise ApiServerError(
                message,
                status=status,
                payload=payload,
                context=ctx,
            )
        raise ApiError(message, status=status, payload=payload, context=ctx)

    @staticmethod
    def _json(resp: requests.Response) -> Dict[str, Any]:
        """Parse object JSON response.

        Args:
            resp: HTTP response.

        Returns:
            Parsed JSON object.

        Raises:
            RuntimeError: If payload is not a JSON object.
        """
        data = JobRestAdapter._json_any(resp)
        if not isinstance(data, dict):
            raise RuntimeError("Invalid JSON response: expected object")
        return data

    @staticmethod
    def _json_any(resp: requests.Response) -> Any:
        """Parse arbitrary JSON response payload.

        Args:
            resp: HTTP response.

        Returns:
            Parsed JSON value.

        Raises:
            RuntimeError: If payload is not valid JSON.
        """
        try:
            return resp.json()
        except Exception:
            txt = getattr(resp, "text", "")[:400]
            raise RuntimeError(f"Invalid JSON response: {txt}")
