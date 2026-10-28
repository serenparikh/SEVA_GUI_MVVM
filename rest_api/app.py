"""Primary FastAPI application for the potentiostat REST service.

This module defines the HTTP contract consumed by GUI adapters in `seva`:

- `seva.adapters.device_rest` for discovery and mode metadata
- `seva.adapters.job_rest` for validate/start/poll/cancel/download workflows
- `seva.adapters.firmware_rest` for firmware uploads

The routes here orchestrate job lifecycle state, then delegate specialized work
to helper modules:

- `validation.py` for payload validation
- `progress_utils.py` for progress and remaining-time estimates
- `storage.py` for run-directory naming and lookup
- `nas_smb.py` for SMB upload and retention operations

Error responses are normalized through `http_error(...)` so GUI ViewModels can
surface stable error codes/messages without parsing framework-native payloads.
"""

import logging, os, uuid, threading, zipfile, io, pathlib, datetime, platform, subprocess, shutil
from typing import Optional, Literal, Dict, List, Any
from datetime import timezone
import serial.tools.list_ports
from fastapi import Body, FastAPI, HTTPException, Header, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
import shlex  
import nas_smb as nas  
import asyncio
import json
import math
import random
import time
from dataclasses import dataclass, asdict
from fastapi import Query
from fastapi.responses import StreamingResponse

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover
    try:
        import importlib_metadata  # type: ignore
    except ImportError:  # pragma: no cover
        importlib_metadata = None  # type: ignore

from pyBEEP.controller import (
    connect_to_potentiostats,  # liefert List[PotentiostatController]
    PotentiostatController,
)
# Optional: use existing plot functions
from pyBEEP.plotter import plot_cv_cycles, plot_time_series
from progress_utils import compute_progress, estimate_planned_duration, utcnow_iso
from validation import (
    ValidationResult,
    UnsupportedModeError,
    validate_mode_payload,
)
import storage
from update_package import (
    PackageUpdateManager,
    UpdateApplyError,
    UpdatePackageError,
)

API_KEY = os.getenv("BOX_API_KEY", "")
BOX_ID = os.getenv("BOX_ID", "")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS_ROOT = pathlib.Path(os.getenv("RUNS_ROOT", "/opt/box/runs"))
RUNS_ROOT.mkdir(parents=True, exist_ok=True)
storage.configure_runs_root(RUNS_ROOT)
NAS_CONFIG_PATH = pathlib.Path(os.getenv("NAS_CONFIG_PATH", "/opt/box/nas_smb.json"))
NAS = nas.NASManager(runs_root=RUNS_ROOT, config_path=NAS_CONFIG_PATH, logger=logging.getLogger("nas_smb"))
UPDATES_ROOT = pathlib.Path(os.getenv("UPDATES_ROOT", "/opt/box/updates"))

RunStorageInfo = storage.RunStorageInfo
RUN_DIRECTORY_LOCK = storage.RUN_DIRECTORY_LOCK
RUN_DIRECTORIES = storage.RUN_DIRECTORIES
_run_index_path = storage.run_index_path
_value_or_none = storage.value_or_none
_sanitize_path_segment = storage.sanitize_path_segment
_sanitize_optional_segment = storage.sanitize_optional_segment
_sanitize_client_datetime = storage.sanitize_client_datetime
_record_run_directory = storage.record_run_directory
_forget_run_directory = storage.forget_run_directory
_resolve_run_directory = storage.resolve_run_directory
configure_run_storage_root = storage.configure_runs_root

API_VERSION = "1.0"

try:
    from seva.utils.logging import configure_root as _configure_logging, level_name as _level_name
except Exception:  # pragma: no cover - fallback when GUI package unavailable
    def _configure_logging(default_level: int | str = logging.INFO) -> int:
        """Configure root logging when shared GUI logging helpers are unavailable."""
        level = logging.INFO
        if isinstance(default_level, str):
            candidate = getattr(logging, default_level.upper(), None)
            if isinstance(candidate, int):
                level = candidate
        else:
            try:
                level = int(default_level)
            except Exception:
                level = logging.INFO
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=level,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        logging.getLogger().setLevel(level)
        return level

    def _level_name(level: int) -> str:
        """Return standard logging level name text for diagnostics."""
        return logging.getLevelName(level)

else:
    def _level_name(level: int) -> str:
        """Return standard logging level name text for diagnostics."""
        return logging.getLevelName(level)


_configure_logging()
log = logging.getLogger("rest_api")
log.debug("REST API logger initialized at %s", _level_name(logging.getLogger().level))


def _detect_pybeep_version() -> str:
    """Detect the installed pyBEEP package version for version reporting.
    
    Parameters
    ----------
    None
        This callable does not receive explicit input parameters.
    
    Returns
    -------
    str
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if "importlib_metadata" in globals() and importlib_metadata:
        try:
            version = importlib_metadata.version("pyBEEP")
            if version:
                return version
        except Exception:
            pass
    try:
        import pyBEEP  # type: ignore
    except Exception:
        return "unknown"
    for attr in ("__version__", "VERSION"):
        value = getattr(pyBEEP, attr, None)  # type: ignore[name-defined]
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _detect_build_identifier() -> str:
    """Resolve a build identifier from environment variables, git, or timestamp fallback.
    
    Parameters
    ----------
    None
        This callable does not receive explicit input parameters.
    
    Returns
    -------
    str
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    env_build = os.getenv("BOX_BUILD") or os.getenv("BOX_BUILD_ID")
    if env_build:
        return env_build
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo_root),
        )
        commit = (result.stdout or "").strip()
        if commit:
            return commit
    except Exception:
        pass
    return datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_error_detail(code: str, message: str, hint: Optional[str] = None) -> Dict[str, str]:
    """Build the canonical JSON error payload returned by API endpoints.
    
    Parameters
    ----------
    code : str
        Value supplied by the API caller or internal orchestration.
    message : str
        Value supplied by the API caller or internal orchestration.
    hint : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Dict[str, str]
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    return {"code": code, "message": message, "hint": hint or ""}


def http_error(status_code: int, code: str, message: str, hint: Optional[str] = None) -> JSONResponse:
    """Create a JSONResponse in the shared API error format.
    
    Parameters
    ----------
    status_code : int
        Value supplied by the API caller or internal orchestration.
    code : str
        Value supplied by the API caller or internal orchestration.
    message : str
        Value supplied by the API caller or internal orchestration.
    hint : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    JSONResponse
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if status_code == 422:
        log.info("Validation failed [%s]: %s", code, message)
        if hint:
            log.debug("Validation hint: %s", hint)
    return JSONResponse(status_code=status_code, content=_build_error_detail(code, message, hint))


PYBEEP_VERSION = _detect_pybeep_version()
PYTHON_VERSION = platform.python_version()
BUILD_IDENTIFIER = _detect_build_identifier()

# ---------- Device registry ----------
class DeviceInfo(BaseModel):
    """Schema describing one discovered hardware slot.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    slot: str
    port: str  # e.g. /dev/ttyACM0 or ttyACM0
    sn: Optional[str] = None

DEVICES: Dict[str, PotentiostatController] = {}   # slot -> controller
DEV_META: Dict[str, DeviceInfo] = {}              # slot -> info
DEVICE_SCAN_LOCK = threading.Lock()

def discover_devices():
    """Scan connected potentiostats and refresh in-memory slot metadata.
    
    Parameters
    ----------
    None
        This callable does not receive explicit input parameters.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    with DEVICE_SCAN_LOCK:
        DEVICES.clear()
        DEV_META.clear()
        controllers = connect_to_potentiostats()
        ports = {p.device: p for p in serial.tools.list_ports.comports()}

        for i, ctrl in enumerate(controllers, start=1):
            slot = f"slot{i:02d}"
            DEVICES[slot] = ctrl
            try:
                port_name = ctrl.device.device.serial.port
                serial_info = ports.get(port_name)
                serial_number = serial_info.serial_number if serial_info else None
            except Exception:
                port_name, serial_number = "<unknown>", None

            DEV_META[slot] = DeviceInfo(slot=slot, port=str(port_name), sn=serial_number)

# ---------- Job models ----------
class JobRequest (BaseModel):
    """Schema for `/jobs` start requests from GUI clients.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    devices: List[str] | Literal["all"] = Field(..., description='e.g. ["slot01","slot02"] or "all"')
    modes: List[str] = Field(..., min_length=1, description="e.g. ['CV','EIS']")
    params_by_mode: Dict[str, Dict] = Field(default_factory=dict, description="per-mode parameter schema")
    tia_gain: Optional[int] = 0
    sampling_interval: Optional[float] = None
    experiment_name: str = Field(..., description="Experiment name for storage")
    subdir: Optional[str] = Field(default=None, description="Optional subfolder for storage")
    client_datetime: str = Field(..., description="Client timestamp for directory and file names")
    run_name: Optional[str] = None
    folder_name: Optional[str] = None
    group_id: Optional[str] = Field(default=None, description="Optional group label for /jobs?group_id")
    make_plot: bool = True


def _build_run_storage_info(req: JobRequest) -> RunStorageInfo:
    """Derive sanitized storage naming metadata from a job request.
    
    Parameters
    ----------
    req : JobRequest
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    RunStorageInfo
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    subdir_source = req.subdir
    if _value_or_none(subdir_source) is None:
        subdir_source = req.folder_name

    experiment_segment = _sanitize_path_segment(req.experiment_name, "experiment_name")
    subdir_segment = _sanitize_optional_segment(subdir_source)
    timestamp_segment = _sanitize_client_datetime(req.client_datetime)
    timestamp_name = timestamp_segment.replace("T", "_")

    filename_parts = [experiment_segment]
    if subdir_segment:
        filename_parts.append(subdir_segment)
    filename_parts.append(timestamp_name)
    filename_prefix = "_".join(filename_parts)

    return RunStorageInfo(
        experiment=experiment_segment,
        subdir=subdir_segment,
        timestamp_dir=timestamp_segment,
        timestamp_name=timestamp_name,
        filename_prefix=filename_prefix,
    )


class SlotStatus(BaseModel):
    """Schema for per-slot execution state inside a run.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    slot: str
    status: Literal["idle", "queued", "running", "done", "failed", "cancelled"]
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    message: Optional[str] = None
    files: List[str] = Field(default_factory=list)  # relative paths

class JobStatus(BaseModel):
    """Schema for full run status responses.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    run_id: str
    # For backward compatibility we use 'mode' as the *current* mode
    mode: str
    started_at: str
    status: Literal["running", "done", "failed", "cancelled"]
    ended_at: Optional[str] = None
    slots: List[SlotStatus]
    progress_pct: int = 0
    remaining_s: Optional[int] = None
    modes: List[str] = Field(default_factory=list)
    current_mode: Optional[str] = None
    remaining_modes: List[str] = Field(default_factory=list)


class JobOverview(BaseModel):
    """Schema for compact run listings returned by `/jobs`.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    run_id: str
    mode: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    devices: List[str]


class JobStatusBulkRequest(BaseModel):
    """Schema for bulk status lookup requests.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    run_ids: List[str] = Field(..., min_length=1, description="run_id list for bulk status lookup")

JOBS: Dict[str, JobStatus] = {}            # run_id -> status
JOB_LOCK = threading.Lock()
SLOT_STATE_LOCK = threading.Lock()
SLOT_RUNS: Dict[str, str] = {}             # slot -> run_id
JOB_META: Dict[str, Dict[str, Any]] = {}   # run_id -> metadata bag
JOB_GROUP_IDS: Dict[str, str] = {}         # run_id -> provided group identifier (raw)
JOB_GROUP_FOLDERS: Dict[str, str] = {}     # run_id -> sanitized storage folder name
CANCEL_FLAGS: Dict[str, threading.Event] = {}  # run_id -> cancel flag


def record_job_meta(run_id: str, mode: str, params: Dict[str, Any]) -> None:
    """Persist the original request parameters and derived duration estimate."""
    JOB_META[run_id] = {
        "mode": mode,
        "params": dict(params or {}),
        "planned_duration_s": estimate_planned_duration(mode, params or {}),
    }


def _normalize_group_value(value: Optional[str]) -> Optional[str]:
    """Normalize optional group identifiers for consistent matching.
    
    Parameters
    ----------
    value : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Optional[str]
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    trimmed = value.strip()
    return trimmed or None


def _derive_group_folder(run_id: str) -> Optional[str]:
    """Infer the storage group folder from a run identifier.
    
    Parameters
    ----------
    run_id : str
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Optional[str]
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    try:
        run_dir = _resolve_run_directory(run_id)
    except HTTPException:
        return None
    except Exception:
        return None
    try:
        relative = run_dir.relative_to(RUNS_ROOT)
    except Exception:
        return None
    parts = relative.parts
    if len(parts) >= 3:
        return parts[-2]
    return None


def _job_overview_status(job: JobStatus) -> Literal["queued", "running", "done", "failed", "cancelled"]:
    """Compute list-level status semantics from per-slot states.
    
    Parameters
    ----------
    job : JobStatus
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Literal['queued', 'running', 'done', 'failed', 'cancelled']
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    slot_states = [slot.status for slot in job.slots]
    if slot_states and all(state == "queued" for state in slot_states):
        return "queued"
    return job.status


def job_snapshot(job: JobStatus) -> JobStatus:
    """Build a status snapshot enriched with computed progress metrics.
    
    Parameters
    ----------
    job : JobStatus
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    JobStatus
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    copy = job.model_copy(deep=True)

    # only create/retain meta while the job is running
    meta = JOB_META.get(copy.run_id)
    if meta is None and copy.status == "running":
        meta = JOB_META[copy.run_id] = {"mode": copy.mode, "params": {}}
    elif meta is None:
        meta = {"mode": copy.mode, "params": {}}

    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    planned = meta.get("planned_duration_s")
    if planned is None:
        planned = estimate_planned_duration(meta.get("mode") or copy.mode, params)
        # store planned only for running jobs to avoid re-populating after cleanup
        if copy.status == "running":
            meta["planned_duration_s"] = planned

    slot_payload = [slot.model_dump() for slot in copy.slots]
    metrics = compute_progress(
        status=copy.status,
        slots=slot_payload,
        started_at=copy.started_at,
        planned_duration_s=planned,
    )
    copy.progress_pct = metrics.get("progress_pct") or 0
    copy.remaining_s = metrics.get("remaining_s")
    return copy


# ---------- Startup ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown side effects.
    
    Parameters
    ----------
    app : FastAPI
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    discover_devices()
    try:
        NAS.start_background()
    except Exception:
        log.exception("Failed to start NAS background tasks")
    try:
        yield
    finally:
        for ctrl in DEVICES.values():
            try:
                ctrl.device.device.serial.close()
            except Exception:
                pass

app = FastAPI(title="Potentiostat Box API", version=API_VERSION, lifespan=lifespan)
# TODO(metrics): optional Prometheus /metrics exporter (future)


@app.exception_handler(RequestValidationError)
async def handle_request_validation(
    request: Request, exc: RequestValidationError
):
    """Map FastAPI validation errors to the API-specific error contract.
    
    Parameters
    ----------
    request : Request
        Value supplied by the API caller or internal orchestration.
    exc : RequestValidationError
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    errors = exc.errors()
    log.info("Validation 422 path=%s issues=%d", request.url.path, len(errors))
    log.debug("Validation detail: %s", errors)
    return JSONResponse(
        status_code=422,
        content={
            "code": "request.validation_error",
            "message": "Validation failed",
            "hint": "Please check request payload and fields.",
            "detail": errors,
        },
    )

# ---------- Auth Helper ----------
def require_key(x_api_key: Optional[str]) -> Optional[JSONResponse]:
    """Enforce optional API-key authentication for protected endpoints.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Optional[JSONResponse]
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if API_KEY and x_api_key != API_KEY:
        return http_error(
            status_code=401,
            code="auth.invalid_api_key",
            message="Unauthorized",
            hint="X-API-Key header is missing or incorrect.",
        )
    return None


class FirmwareFlashRuntimeError(RuntimeError):
    """Typed firmware flashing error used by both REST routes and update jobs."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        hint: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint


def _firmware_storage_dir() -> pathlib.Path:
    """Return configured firmware upload directory."""
    return pathlib.Path(os.getenv("FIRMWARE_STORAGE_DIR", "/opt/box/firmware"))


def _flash_script_path() -> pathlib.Path:
    """Return configured firmware flashing script path."""
    configured = os.getenv("FLASH_SCRIPT_PATH", "")
    if configured.strip():
        return pathlib.Path(configured.strip())
    return pathlib.Path(__file__).resolve().with_name("auto_flash_linux.py")


def _store_uploaded_firmware(file: UploadFile) -> pathlib.Path:
    """Validate and persist uploaded firmware file and return target path."""
    if not file or not file.filename:
        raise FirmwareFlashRuntimeError(
            status_code=400,
            code="firmware.invalid_upload",
            message="Invalid firmware upload",
            hint="Upload a .bin firmware file.",
        )

    original_name = pathlib.Path(file.filename).name
    if not original_name.lower().endswith(".bin"):
        raise FirmwareFlashRuntimeError(
            status_code=400,
            code="firmware.invalid_upload",
            message="Invalid firmware upload",
            hint="Only .bin files are allowed.",
        )

    try:
        sanitized_stem = _sanitize_path_segment(pathlib.Path(original_name).stem, "filename")
    except HTTPException as exc:
        raise FirmwareFlashRuntimeError(
            status_code=400,
            code="firmware.invalid_upload",
            message="Invalid firmware upload",
            hint=str(exc.detail),
        ) from exc

    firmware_dir = _firmware_storage_dir()
    firmware_dir.mkdir(parents=True, exist_ok=True)
    target_path = firmware_dir / f"{sanitized_stem}.bin"
    try:
        with target_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
    except Exception as exc:
        log.exception("Failed to store firmware upload")
        raise FirmwareFlashRuntimeError(
            status_code=500,
            code="firmware.store_failed",
            message="Failed to store firmware file",
            hint=str(exc) or "Check storage permissions and available space.",
        ) from exc
    return target_path


def _run_flash_script(bin_path: pathlib.Path) -> Dict[str, Any]:
    """Run the firmware flashing subprocess for one binary path."""
    script_path = _flash_script_path()
    if not script_path.is_file():
        raise FirmwareFlashRuntimeError(
            status_code=500,
            code="firmware.script_missing",
            message="Firmware flash script missing",
            hint=f"Expected script at {script_path}.",
        )
    result = subprocess.run(
        ["python", script_path.name, str(bin_path)],
        cwd=str(script_path.parent),
        capture_output=True,
        text=True,
    )
    exit_code = result.returncode
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if exit_code != 0:
        raise FirmwareFlashRuntimeError(
            status_code=500,
            code="firmware.flash_failed",
            message="Firmware flash failed",
            hint=(
                "Try increasing the Request timeout (s)\n"
                f"exit_code={exit_code}\nstdout={stdout}\nstderr={stderr}"
            ),
        )
    return {
        "ok": True,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def _flash_firmware_binary(bin_path: pathlib.Path) -> Dict[str, Any]:
    """Shared firmware flashing core reused by both API routes and update worker."""
    path = pathlib.Path(bin_path)
    if not path.is_file():
        raise FirmwareFlashRuntimeError(
            status_code=400,
            code="firmware.invalid_upload",
            message="Invalid firmware upload",
            hint=f"Firmware file not found: {path}",
        )
    if path.suffix.lower() != ".bin":
        raise FirmwareFlashRuntimeError(
            status_code=400,
            code="firmware.invalid_upload",
            message="Invalid firmware upload",
            hint="Only .bin files are allowed.",
        )
    return _run_flash_script(path)


def _flash_firmware_for_update(bin_path: pathlib.Path) -> Dict[str, Any]:
    """Update-worker wrapper that maps firmware errors to update apply errors."""
    try:
        return _flash_firmware_binary(bin_path)
    except FirmwareFlashRuntimeError as exc:
        raise UpdateApplyError(
            code=exc.code,
            message=exc.message,
            hint=exc.hint,
            status_code=exc.status_code,
        ) from exc


def _restart_service_for_update() -> Dict[str, Any]:
    """Execute configured service restart command after successful apply."""
    command_text = os.getenv("BOX_RESTART_COMMAND", "systemctl restart pybeep-box.service")
    args = shlex.split(command_text)
    if not args:
        raise UpdateApplyError(
            code="updates.restart_failed",
            message="Service restart command is empty",
            hint="Set BOX_RESTART_COMMAND to a non-empty restart command.",
        )
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise UpdateApplyError(
            code="updates.restart_failed",
            message="Service restart command not found",
            hint=str(exc),
        ) from exc
    allowed_codes_env = os.getenv("BOX_RESTART_ALLOWED_EXIT_CODES", "")
    allowed_codes = set()
    if allowed_codes_env.strip():
        for token in allowed_codes_env.split(","):
            stripped = token.strip()
            if not stripped:
                continue
            try:
                allowed_codes.add(int(stripped))
            except ValueError:
                log.warning("Ignoring invalid BOX_RESTART_ALLOWED_EXIT_CODES token: %s", stripped)
    allowed_codes.add(-15)
    ok = result.returncode == 0 or result.returncode in allowed_codes
    return {
        "ok": ok,
        "command": command_text,
        "exit_code": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "allowed_exit_codes": sorted(allowed_codes),
    }


UPDATES_MANAGER = PackageUpdateManager(
    repo_root=REPO_ROOT,
    staging_root=UPDATES_ROOT / "staging",
    audit_root=UPDATES_ROOT / "audit",
    flash_firmware=_flash_firmware_for_update,
    restart_service=_restart_service_for_update,
    logger=logging.getLogger("rest_api.updates"),
)


@app.get("/version")
def version_info() -> Dict[str, str]:
    """Return API, runtime, and build version metadata.
    
    Parameters
    ----------
    None
        This callable does not receive explicit input parameters.
    
    Returns
    -------
    Dict[str, str]
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    preferred = UPDATES_MANAGER.preferred_component_versions()
    return {
        "api": preferred.get("rest_api") or API_VERSION,
        "pybeep": preferred.get("pybeep") or PYBEEP_VERSION,
        "firmware": preferred.get("firmware") or "unknown",
        "python": PYTHON_VERSION,
        "build": BUILD_IDENTIFIER,
    }

# ---------- Health / Devices / Modes ----------
@app.get("/health")
def health(x_api_key: Optional[str] = Header(None)):
    """Return service health and discovered-device count.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    with DEVICE_SCAN_LOCK:
        device_count = len(DEVICES)
    return {"ok": True, "devices": device_count, "box_id": BOX_ID}

@app.get("/devices")
def list_devices(x_api_key: Optional[str] = Header(None)):
    """Return discovered device metadata and slot names.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    with DEVICE_SCAN_LOCK:
        slots = sorted(DEV_META.keys())
        return {
            "devices": [DEV_META[s].model_dump() for s in slots],
            "slots": slots,
            "count": len(slots),
        }

@app.get("/devices/status", response_model=List[SlotStatus])
def list_device_status(x_api_key: Optional[str] = Header(None)) -> List[SlotStatus]:
    """Return per-slot runtime state derived from active jobs.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    List[SlotStatus]
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    with DEVICE_SCAN_LOCK:
        slots = sorted(DEV_META.keys())

    with SLOT_STATE_LOCK:
        slot_runs = {slot: SLOT_RUNS.get(slot) for slot in slots}

    results: List[SlotStatus] = []
    with JOB_LOCK:
        for slot in slots:
            run_id = slot_runs.get(slot)
            if not run_id:
                results.append(SlotStatus(slot=slot, status="idle"))
                continue
            job = JOBS.get(run_id)
            if not job:
                results.append(SlotStatus(slot=slot, status="idle"))
                continue
            slot_status = next(
                (entry for entry in job.slots if entry.slot == slot),
                None,
            )
            if slot_status:
                results.append(slot_status.model_copy(deep=True))
            else:
                results.append(SlotStatus(slot=slot, status="idle"))
    return results

@app.get("/modes")
def list_modes(x_api_key: Optional[str] = Header(None)):
    """Expose mode names supported by the connected controller.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    # Take modes from the first device (all are configured identically)
    with DEVICE_SCAN_LOCK:
        try:
            first = next(iter(DEVICES.values()))
        except StopIteration:
            return http_error(
                status_code=503,
                code="devices.unavailable",
                message="No devices registered",
                hint="Use /admin/rescan to look for new devices.",
            )
    return first.get_available_modes()

@app.get("/modes/{mode}/params")
def mode_params(mode: str, x_api_key: Optional[str] = Header(None)):
    """Return mode-specific parameter schema information.
    
    Parameters
    ----------
    mode : str
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    with DEVICE_SCAN_LOCK:
        try:
            first = next(iter(DEVICES.values()))
        except StopIteration:
            return http_error(
                status_code=503,
                code="devices.unavailable",
                message="No devices registered",
                hint="Use /admin/rescan to look for new devices.",
            )
    try:
        return {k: str(v) for k, v in first.get_mode_params(mode).items()}
    except Exception as e:
        return http_error(
            status_code=400,
            code="modes.parameter_error",
            message=str(e),
            hint="Check parameters according to the mode specification.",
        )


@app.post("/modes/{mode}/validate")
def validate_mode_params(
    mode: str,
    params: Dict[str, Any] = Body(...),
    x_api_key: Optional[str] = Header(None),
) -> ValidationResult:
    """Validate mode parameter payloads without contacting any hardware."""

    if auth_error := require_key(x_api_key):
        return auth_error

    try:
        return validate_mode_payload(mode, params or {})
    except UnsupportedModeError as exc:
        return http_error(
            status_code=404,
            code="modes.not_found",
            message=str(exc),
            hint="Fetch available modes via /modes.",
        )

# ---------- Job Worker ----------
def _update_job_status_locked(job: Optional[JobStatus]) -> None:
    """Recompute aggregate job state from per-slot statuses.
    
    Parameters
    ----------
    job : Optional[JobStatus]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    None
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if not job:
        return

    statuses = [slot.status for slot in job.slots]
    if any(state in ("queued", "running") for state in statuses):
        job.status = "running"
        job.ended_at = None
        return

    if any(state == "failed" for state in statuses):
        job.status = "failed"
    elif any(state == "cancelled" for state in statuses):
        job.status = "cancelled"
    else:
        job.status = "done"
    job.ended_at = utcnow_iso()
    #drop transient meta once job is terminal
    JOB_META.pop(job.run_id, None)
    CANCEL_FLAGS.pop(job.run_id, None)
    # NEW: enqueue upload only for 'done' (not for failed/cancelled)
    if job.status == "done":
        try:
            NAS.enqueue_upload(job.run_id)
        except Exception:
            log.exception("Failed to enqueue NAS upload for run_id=%s", job.run_id)


def _request_controller_abort(ctrl: PotentiostatController) -> None:
    """Best effort attempt to stop a running measurement on the controller."""
    for attr in (
        "abort_measurement",
        "cancel_measurement",
        "stop_measurement",
        "abort",
        "cancel",
        "stop",
    ):
        method = getattr(ctrl, attr, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            else:
                return

    try:
        serial = getattr(ctrl, "device", None)
        serial = getattr(serial, "device", serial)
        serial = getattr(serial, "serial", serial)
        close_method = getattr(serial, "close", None)
        if callable(close_method):
            close_method()
    except Exception:
        pass

def _run_slot_sequence(
    run_id: str,
    run_dir: pathlib.Path,
    slot: str,
    req: JobRequest,
    slot_status: SlotStatus,
    storage: RunStorageInfo,
):
    """Runs the 'modes' list sequentially. Each measurement writes to its own mode subfolder."""
    ctrl = DEVICES[slot]
    slot_segment = _sanitize_path_segment(slot, "slot")
    cancel_event = CANCEL_FLAGS.setdefault(run_id, threading.Event())

    def _eval_plot(csv_path: pathlib.Path, mode: str, params: Dict[str, Any]) -> List[str]:
        """Generate optional plot artifact and return sorted relative output file list."""
        files: List[str] = []
        try:
            if req.make_plot:
                png_path = csv_path.with_suffix(".png")
                if (mode or "").upper() == "CV":
                    plot_cv_cycles(str(csv_path), figpath=str(png_path), show=False, cycles=params.get("cycles"))
                else:
                    plot_time_series(str(csv_path), figpath=str(png_path), show=False)
            folder = csv_path.parent
            files = [str(p.relative_to(run_dir)) for p in folder.iterdir() if p.is_file()]
        except Exception:
            try:
                folder = csv_path.parent
                files = [str(p.relative_to(run_dir)) for p in folder.iterdir() if p.is_file()]
            except Exception:
                files = []
        return sorted(files)

    # Set slot to running initially (consistent)
    with JOB_LOCK:
        slot_status.status = "running"
        slot_status.started_at = slot_status.started_at or utcnow_iso()
        slot_status.message = None
        job = JOBS.get(run_id)
        if job:
            job.status = "running"
            job.ended_at = None

    files_collected: List[str] = []
    error: Optional[str] = None

    try:
        for idx, mode in enumerate(req.modes or []):
            if cancel_event.is_set():
                error = "cancelled"
                break

            # Status: set current/remaining mode (also 'mode' for compatibility)
            with JOB_LOCK:
                job = JOBS.get(run_id)
                if job:
                    job.mode = mode
                    job.current_mode = mode
                    job.modes = list(req.modes or [])
                    job.remaining_modes = list(req.modes[idx + 1:])

            # Per-mode folders/filenames
            mode_segment = _sanitize_path_segment(mode, "mode")
            mode_dir = run_dir / "Wells" / slot_segment / mode_segment
            mode_dir.mkdir(parents=True, exist_ok=True)
            filename_base = f"{storage.filename_prefix}_{slot_segment}_{mode_segment}"
            filename = f"{filename_base}.csv"

            params = dict(req.params_by_mode.get(mode, {}) or {})
            # Mode-level override (currently only CV sets this) takes
            # precedence over the job-wide default; pop it so it never
            # reaches pyBEEP's per-mode parameter validation.
            mode_sampling_interval = params.pop("sampling_interval", None)
            effective_sampling_interval = (
                mode_sampling_interval if mode_sampling_interval is not None else req.sampling_interval
            )

            # Measurement with abort window in background thread
            measurement_error: Optional[Exception] = None
            def _runner():
                """Run one mode measurement and capture exceptions for outer thread."""
                nonlocal measurement_error
                try:
                    ctrl.apply_measurement(
                        mode=mode,
                        params=params,
                        tia_gain=req.tia_gain,
                        sampling_interval=effective_sampling_interval,
                        filename=filename,
                        folder=str(mode_dir),
                    )
                except Exception as exc:
                    measurement_error = exc

            t = threading.Thread(target=_runner, name=f"{run_id}-{slot}-{mode}", daemon=True)
            t.start()
            abort_requested = False
            while t.is_alive():
                t.join(timeout=0.2)
                if cancel_event.is_set() and not abort_requested:
                    _request_controller_abort(ctrl)
                    abort_requested = True
            t.join()

            if cancel_event.is_set():
                error = "cancelled"
                break

            if measurement_error:
                error = str(measurement_error)
                break

            # Collect files, advance status
            csv_path = mode_dir / filename
            files_collected.extend(_eval_plot(csv_path, mode, params))

    except Exception as exc:
        error = str(exc)

    # Slot/JOB finalisieren
    with JOB_LOCK:
        if error == "cancelled":
            slot_status.status = "cancelled"
            slot_status.message = "cancelled"
        elif error:
            slot_status.status = "failed"
            slot_status.message = error
        else:
            slot_status.status = "done"
            slot_status.message = None

        slot_status.ended_at = utcnow_iso()
        slot_status.files = sorted(files_collected)

        job = JOBS.get(run_id)
        if job:
            # If last slot finished, terminate job + reset modes fields
            _update_job_status_locked(job)
            if job.status in ("done", "failed", "cancelled"):
                job.current_mode = None
                job.remaining_modes = []
                
    with SLOT_STATE_LOCK:
        if SLOT_RUNS.get(slot) == run_id:
            del SLOT_RUNS[slot]


def _run_one_slot(
    run_id: str,
    run_dir: pathlib.Path,
    slot: str,
    req: JobRequest,
    slot_status: SlotStatus,
    storage: RunStorageInfo,
):
    """Process one slot/device - blocking in the thread."""
    ctrl = DEVICES[slot]
    slot_segment = _sanitize_path_segment(slot, "slot")
    mode_segment = _sanitize_path_segment(req.mode, "mode")
    slot_dir = run_dir / "Wells" / slot_segment / mode_segment
    slot_dir.mkdir(parents=True, exist_ok=True)
    filename_base = f"{storage.filename_prefix}_{slot_segment}_{mode_segment}"
    filename = f"{filename_base}.csv"

    cancel_event = CANCEL_FLAGS.setdefault(run_id, threading.Event())

    if cancel_event.is_set():
        with JOB_LOCK:
            slot_status.status = "cancelled"
            if not slot_status.started_at:
                slot_status.started_at = utcnow_iso()
            slot_status.ended_at = utcnow_iso()
            slot_status.message = "cancelled"
            slot_status.files = []
            _update_job_status_locked(JOBS.get(run_id))
        with SLOT_STATE_LOCK:
            if SLOT_RUNS.get(slot) == run_id:
                del SLOT_RUNS[slot]
        return

    with JOB_LOCK:
        slot_status.status = "running"
        slot_status.started_at = utcnow_iso()
        slot_status.message = None
        _update_job_status_locked(JOBS.get(run_id))

    files: List[str] = []
    error: Optional[Exception] = None
    cancelled = False
    measurement_error: Optional[Exception] = None

    def _measurement_runner():
        """Execute measurement call and capture raised exception for polling loop."""
        nonlocal measurement_error
        try:
            ctrl.apply_measurement(
                mode=req.mode,
                params=req.params,
                tia_gain=req.tia_gain,
                sampling_interval=req.sampling_interval,
                filename=filename,
                folder=str(slot_dir),
            )
        except Exception as exc:
            measurement_error = exc

    runner_thread = threading.Thread(
        target=_measurement_runner,
        name=f"{run_id}-{slot}-measurement",
        daemon=True,
    )
    runner_thread.start()

    abort_requested = False
    while runner_thread.is_alive():
        runner_thread.join(timeout=0.2)
        if cancel_event.is_set():
            cancelled = True
            if not abort_requested:
                _request_controller_abort(ctrl)
                abort_requested = True

    runner_thread.join()
    if cancel_event.is_set():
        cancelled = True

    if cancelled:
        # treat controller exceptions as part of the cancellation flow
        measurement_error = None

    if measurement_error is not None:
        error = measurement_error
    elif not cancelled:
        csv_path = slot_dir / filename
        if req.make_plot:
            png_path = csv_path.with_suffix('.png')
            if req.mode.upper() == "CV":
                plot_cv_cycles(str(csv_path), figpath=str(png_path), show=False, cycles=req.params.get("cycles"))
            else:
                plot_time_series(str(csv_path), figpath=str(png_path), show=False)
        files = [
            str(p.relative_to(run_dir))
            for p in slot_dir.iterdir()
            if p.is_file()
        ]
    else:
        try:
            files = [
                str(p.relative_to(run_dir))
                for p in slot_dir.iterdir()
                if p.is_file()
            ]
        except Exception:
            files = []

    with JOB_LOCK:
        if cancelled:
            slot_status.status = "cancelled"
            slot_status.ended_at = utcnow_iso()
            slot_status.message = "cancelled"
            slot_status.files = sorted(files)
        elif error is None:
            slot_status.files = sorted(files)
            slot_status.status = "done"
            slot_status.ended_at = utcnow_iso()
            slot_status.message = None
        else:
            slot_status.status = "failed"
            slot_status.message = str(error)
            slot_status.ended_at = utcnow_iso()
            try:
                slot_status.files = sorted([str(p.relative_to(run_dir)) for p in slot_dir.iterdir() if p.is_file()])
            except Exception:
                slot_status.files = []
        _update_job_status_locked(JOBS.get(run_id))

    with SLOT_STATE_LOCK:
        if SLOT_RUNS.get(slot) == run_id:
            del SLOT_RUNS[slot]

# ---------- Endpunkte: Jobs ----------
@app.post("/jobs/status", response_model=List[JobStatus])
def jobs_bulk_status(req: JobStatusBulkRequest, x_api_key: Optional[str] = Header(None)):
    """Return snapshot data for multiple runs in a single call."""
    if auth_error := require_key(x_api_key):
        return auth_error
    run_ids = [rid for rid in (req.run_ids or []) if rid]
    if not run_ids:
        return http_error(
            status_code=400,
            code="jobs.missing_run_ids",
            message="No run_ids provided",
            hint="Fill in the run_ids field in the request.",
        )
    with JOB_LOCK:
        missing = [rid for rid in run_ids if rid not in JOBS]
        if missing:
            missing_str = ", ".join(sorted(missing))
            return http_error(
                status_code=404,
                code="jobs.run_ids_unknown",
                message=f"Unbekannte run_ids: {missing_str}",
                hint="Nur bekannte run_ids anfragen.",
            )
        snapshots = [job_snapshot(JOBS[rid]) for rid in run_ids]
    log.debug("jobs/status bulk request count=%d", len(run_ids))
    return snapshots


@app.get("/jobs", response_model=List[JobOverview])
def list_jobs(
    state: Optional[Literal["incomplete", "completed"]] = None,
    group_id: Optional[str] = None,
    x_api_key: Optional[str] = Header(None),
) -> List[JobOverview]:
    """Return a lightweight job overview list with optional filtering."""
    if auth_error := require_key(x_api_key):
        return auth_error
    state_filter = state or None
    group_filter = _normalize_group_value(group_id)
    group_filter_lower = group_filter.lower() if group_filter else None

    with JOB_LOCK:
        run_ids = list(JOBS.keys())
        job_entries = [(rid, JOBS[rid].model_copy(deep=True)) for rid in run_ids]
        group_raw_map = {rid: JOB_GROUP_IDS.get(rid) for rid in run_ids}
        group_folder_map = {rid: JOB_GROUP_FOLDERS.get(rid) for rid in run_ids}

    results: List[JobOverview] = []
    for run_id, job in job_entries:
        overview_status = _job_overview_status(job)
        if state_filter == "incomplete" and overview_status not in ("queued", "running"):
            continue
        if state_filter == "completed" and overview_status not in ("done", "failed", "cancelled"):
            continue

        if group_filter_lower:
            candidate_norms = set()
            for candidate in (
                group_raw_map.get(run_id),
                group_folder_map.get(run_id),
            ):
                normalized_candidate = _normalize_group_value(candidate)
                if normalized_candidate:
                    candidate_norms.add(normalized_candidate.lower())
            normalized_folder = _normalize_group_value(_derive_group_folder(run_id))
            if normalized_folder:
                candidate_norms.add(normalized_folder.lower())
            if group_filter_lower not in candidate_norms:
                continue

        devices = [slot.slot for slot in job.slots]
        results.append(
            JobOverview(
                run_id=run_id,
                mode=job.mode,
                status=overview_status,
                started_at=job.started_at,
                ended_at=job.ended_at,
                devices=devices,
            )
        )

    results.sort(key=lambda item: ((item.started_at or ""), item.run_id), reverse=True)
    return results


@app.post("/jobs", response_model=JobStatus)
def start_job(req: JobRequest, x_api_key: Optional[str] = Header(None)):
    """Start a new job across selected slots and launch worker threads (multi-mode sequence)."""
    if auth_error := require_key(x_api_key):
        return auth_error

    if not req.modes:
        return http_error(status_code=422, code="jobs.invalid_request", message="modes must not be empty")
    for m in req.modes:
        if m not in req.params_by_mode:
            return http_error(status_code=422, code="jobs.invalid_request", message=f"missing params for mode {m}")

    with DEVICE_SCAN_LOCK:
        if req.devices == "all":
            slots = sorted(DEVICES.keys())
        else:
            slots = [s for s in req.devices if s in DEVICES]
    if not slots:
        return http_error(status_code=400, code="jobs.invalid_devices", message="No valid devices specified", hint="Use slots from /devices or 'all'.")

    run_id = req.run_name or datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]

    with JOB_LOCK:
        if run_id in JOBS:
            return http_error(status_code=409, code="jobs.run_id_conflict", message="run_id already active", hint="Choose another run_id or wait for the running job.")

    with SLOT_STATE_LOCK:
        busy = sorted(s for s in slots if s in SLOT_RUNS)
        if busy:
            return http_error(status_code=409, code="jobs.slots_busy", message=f"Slots busy: {', '.join(busy)}", hint="Wait until the listed slots are free.")
        for s in slots:
            SLOT_RUNS[s] = run_id

    slot_statuses = [SlotStatus(slot=s, status="queued") for s in slots]
    started_at = utcnow_iso()

    try:
        storage_info = _build_run_storage_info(req)
        path_parts = [p for p in (storage_info.experiment, storage_info.subdir) if p]
        path_parts.append(storage_info.timestamp_dir)
        run_dir = RUNS_ROOT.joinpath(*path_parts)
        run_dir.mkdir(parents=True, exist_ok=True)
        _record_run_directory(run_id, run_dir)

        raw_group_id = (
            _normalize_group_value(req.group_id)
            or _normalize_group_value(req.folder_name)
            or _normalize_group_value(req.subdir)
        )
        storage_folder = storage_info.subdir

        # For compatibility: 'mode' = first mode, 'modes' full list
        first_mode = (req.modes or [""])[0]
        job = JobStatus(
            run_id=run_id,
            mode=first_mode,
            started_at=started_at,
            status="running",
            ended_at=None,
            slots=slot_statuses,
            modes=list(req.modes or []),
            current_mode=first_mode,
            remaining_modes=list(req.modes[1:] if len(req.modes) > 1 else []),
        )

        with JOB_LOCK:
            JOBS[run_id] = job
            CANCEL_FLAGS[run_id] = threading.Event()
            # Progress estimate based on the first mode (KISS)
            record_job_meta(run_id, first_mode, dict(req.params_by_mode.get(first_mode, {}) or {}))
            if raw_group_id:
                JOB_GROUP_IDS[run_id] = raw_group_id
            else:
                JOB_GROUP_IDS.pop(run_id, None)
            if storage_folder:
                JOB_GROUP_FOLDERS[run_id] = storage_folder
            else:
                JOB_GROUP_FOLDERS.pop(run_id, None)

        log.info("Job start run_id=%s modes=%s devices=%s slots=%s", run_id, req.modes, req.devices if req.devices != "all" else "all", slots)
        log.debug("Job storage run_id=%s group_id=%s folder=%s experiment=%s", run_id, raw_group_id or "-", storage_folder or "-", storage_info.experiment)

        for slot_status in slot_statuses:
            t = threading.Thread(
                target=_run_slot_sequence,
                args=(run_id, run_dir, slot_status.slot, req, slot_status, storage_info),
                daemon=True,
            )
            t.start()
    except Exception:
        with SLOT_STATE_LOCK:
            for s in slots:
                if SLOT_RUNS.get(s) == run_id:
                    del SLOT_RUNS[s]
        with JOB_LOCK:
            JOBS.pop(run_id, None)
            JOB_GROUP_IDS.pop(run_id, None)
            JOB_GROUP_FOLDERS.pop(run_id, None)
        CANCEL_FLAGS.pop(run_id, None)
        JOB_META.pop(run_id, None)
        _forget_run_directory(run_id)
        raise

    with JOB_LOCK:
        return job_snapshot(JOBS[run_id])



@app.post("/jobs/{run_id}/cancel", status_code=202)
def cancel_job(run_id: str, x_api_key: Optional[str] = Header(None)):
    """Signal cancellation for a running or queued job."""
    if auth_error := require_key(x_api_key):
        return auth_error
    with JOB_LOCK:
        job = JOBS.get(run_id)
        if not job:
            return http_error(
                status_code=404,
                code="jobs.not_found",
                message="Unbekannte run_id",
                hint="Check run_id or fetch the job list.",
            )

        event = CANCEL_FLAGS.get(run_id)
        if event is None:
            event = CANCEL_FLAGS[run_id] = threading.Event()

        if job.status in ("done", "failed", "cancelled"):
            return {"run_id": run_id, "status": job.status}

        event.set()
        queued_slots: List[str] = []
        for slot_status in job.slots:
            if slot_status.status == "queued":
                slot_status.status = "cancelled"
                if not slot_status.started_at:
                    slot_status.started_at = utcnow_iso()
                slot_status.ended_at = utcnow_iso()
                slot_status.message = "cancelled"
                slot_status.files = []
                queued_slots.append(slot_status.slot)

        _update_job_status_locked(job)

    if queued_slots:
        with SLOT_STATE_LOCK:
            for slot in queued_slots:
                if SLOT_RUNS.get(slot) == run_id:
                    del SLOT_RUNS[slot]

    log.info("Job cancel requested run_id=%s queued_slots=%d", run_id, len(queued_slots))
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/jobs/{run_id}", response_model=JobStatus)
def job_status(run_id: str, x_api_key: Optional[str] = Header(None)):
    """Return the latest status snapshot for a single run."""
    if auth_error := require_key(x_api_key):
        return auth_error
    with JOB_LOCK:
        job = JOBS.get(run_id)
        if not job:
            return http_error(
                status_code=404,
                code="jobs.not_found",
                message="Unbekannte run_id",
                hint="Check run_id or fetch the job list.",
            )
        return job_snapshot(job)


@app.get("/runs/{run_id}/files")
def list_run_files(run_id: str, x_api_key: Optional[str] = Header(None)):
    """List files available inside a run output directory.
    
    Parameters
    ----------
    run_id : str
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    run_dir = _resolve_run_directory(run_id)
    if not run_dir.is_dir():
        return http_error(
            status_code=404,
            code="runs.not_found",
            message="Run not found",
            hint="Check run_id or list existing runs.",
        )
    files = [
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file()
    ]
    files.sort()
    log.info("List files run_id=%s count=%d", run_id, len(files))
    return {"files": files}


@app.get("/runs/{run_id}/file")
def get_run_file(run_id: str, path: str, x_api_key: Optional[str] = Header(None)):
    """Serve a single file from a run output directory.
    
    Parameters
    ----------
    run_id : str
        Value supplied by the API caller or internal orchestration.
    path : str
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    run_dir = _resolve_run_directory(run_id)
    if not run_dir.is_dir():
        return http_error(
            status_code=404,
            code="runs.not_found",
            message="Run not found",
            hint="Check run_id or list existing runs.",
        )
    if not path:
        return http_error(
            status_code=404,
            code="runs.file_not_found",
            message="File not found",
            hint="Provide path relative to the run directory.",
        )

    run_root = run_dir.resolve()
    try:
        target_path = (run_dir / path).resolve(strict=True)
    except FileNotFoundError:
        return http_error(
            status_code=404,
            code="runs.file_not_found",
            message="File not found",
            hint="Provide path relative to the run directory.",
        )

    try:
        target_path.relative_to(run_root)
    except ValueError:
        return http_error(
            status_code=404,
            code="runs.file_not_found",
            message="File not found",
            hint="Provide path relative to the run directory.",
        )

    if not target_path.is_file():
        return http_error(
            status_code=404,
            code="runs.file_not_found",
            message="File not found",
            hint="Provide path relative to the run directory.",
        )

    rel_path = target_path.relative_to(run_root).as_posix()
    log.info("Serve file run_id=%s path=%s", run_id, rel_path)
    return FileResponse(path=target_path, filename=target_path.name)


@app.get("/runs/{run_id}/zip")
def get_run_zip(run_id: str, x_api_key: Optional[str] = Header(None)):
    """Stream a ZIP archive of all files for one run.
    
    Parameters
    ----------
    run_id : str
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    run_dir = _resolve_run_directory(run_id)
    if not run_dir.is_dir():
        return http_error(
            status_code=404,
            code="runs.not_found",
            message="Run not found",
            hint="Check run_id or list existing runs.",
        )
    # Build ZIP in memory
    buf = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in run_dir.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(run_dir))
                file_count += 1
    buf.seek(0)
    content = buf.read()
    log.info("Serve zip run_id=%s files=%d size=%d", run_id, file_count, len(content))
    return Response(content=content,
                    media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.zip"'})

# ---------- NAS Storage Requests ----------

class SMBSetupRequest(BaseModel):
    """Schema for SMB/NAS setup requests.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    host: str
    share: str
    username: str
    password: str
    base_subdir: str = ""     # optional subfolder within the share
    retention_days: int = 14
    domain: Optional[str] = None

@app.post("/nas/setup")
def nas_setup(req: SMBSetupRequest, x_api_key: Optional[str] = Header(None)):
    """Persist NAS/SMB settings and run an initial connectivity probe.
    
    Parameters
    ----------
    req : SMBSetupRequest
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    result = NAS.setup(
        host=req.host,
        share=req.share,
        username=req.username,
        password=req.password,
        base_subdir=req.base_subdir,
        retention_days=req.retention_days,
        domain=req.domain,
    )
    return result

@app.get("/nas/health")
def nas_health(x_api_key: Optional[str] = Header(None)):
    """Report NAS/SMB connectivity status from the manager.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    return NAS.health()

@app.post("/runs/{run_id}/upload")
def nas_upload_run(run_id: str, x_api_key: Optional[str] = Header(None)):
    """Queue an on-demand NAS upload for a run directory.
    
    Parameters
    ----------
    run_id : str
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    enq = NAS.enqueue_upload(run_id)
    return {"ok": True, "enqueued": bool(enq), "run_id": run_id}

# ---------- Admin (optional) ----------
@app.post("/admin/rescan")
def rescan(x_api_key: Optional[str] = Header(None)):
    """Trigger a fresh hardware discovery scan.
    
    Parameters
    ----------
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error
    discover_devices()
    with DEVICE_SCAN_LOCK:
        return {"devices": list(DEVICES.keys())}


@app.post("/updates/package")
def start_package_update(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    """Upload one update package ZIP and enqueue asynchronous apply workflow."""
    if auth_error := require_key(x_api_key):
        return auth_error
    try:
        snapshot = UPDATES_MANAGER.enqueue_upload(
            filename=file.filename or "",
            source=file.file,
        )
    except UpdatePackageError as exc:
        return http_error(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            hint=exc.hint,
        )
    except Exception:
        log.exception("Failed to enqueue package update")
        return http_error(
            status_code=500,
            code="updates.enqueue_failed",
            message="Failed to enqueue package update",
            hint="Inspect server logs for details.",
        )
    return {
        "update_id": snapshot.get("update_id"),
        "status": snapshot.get("status"),
        "step": snapshot.get("step"),
        "queued_at": snapshot.get("created_at"),
    }


@app.get("/updates/{update_id}")
def get_package_update(update_id: str, x_api_key: Optional[str] = Header(None)):
    """Return server-authoritative status for one asynchronous package update."""
    if auth_error := require_key(x_api_key):
        return auth_error
    snapshot = UPDATES_MANAGER.get_job(update_id)
    if snapshot is None:
        return http_error(
            status_code=404,
            code="updates.not_found",
            message="Unknown update_id",
            hint="Check update_id or query GET /updates.",
        )
    return snapshot


@app.get("/updates")
def list_package_updates(
    limit: int = Query(20, ge=1, le=200),
    x_api_key: Optional[str] = Header(None),
):
    """List recent package update jobs (newest first)."""
    if auth_error := require_key(x_api_key):
        return auth_error
    return {"items": UPDATES_MANAGER.list_jobs(limit=limit)}


@app.post("/firmware/flash")
def flash_firmware(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    """Invoke dfu-util to write firmware to the controller.
    
    Parameters
    ----------
    file : UploadFile
        Value supplied by the API caller or internal orchestration.
    x_api_key : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    if auth_error := require_key(x_api_key):
        return auth_error

    try:
        target_path = _store_uploaded_firmware(file)
        result = _flash_firmware_binary(target_path)
    except FirmwareFlashRuntimeError as exc:
        return http_error(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            hint=exc.hint,
        )
    except Exception:
        log.exception("Unexpected firmware flashing failure")
        return http_error(
            status_code=500,
            code="firmware.flash_failed",
            message="Firmware flash failed",
            hint="Inspect server logs for details.",
        )
    return result

# ---------- SEE Stream (Endpoint there) ----------

DEVICE_IDS = list(range(1, 11))

@dataclass
class TemperatureSample:
    """Telemetry sample emitted by the mock source.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    device_id: int
    ts: str          # ISO 8601
    temp_c: float
    seq: int

class LatestResponse(BaseModel):
    """Schema wrapper for latest telemetry samples.
    
    Notes
    -----
    Used by FastAPI routes to validate or serialize request and response payloads.
    """
    samples: List[TemperatureSample]

class MockPotentiostatSource:
    """Generate deterministic-looking mock telemetry for 10 device ids.

    Notes
    -----
    The telemetry endpoint uses this source to exercise GUI streaming workflows
    without connecting to real hardware. Samples include drift, noise, and rare
    dropouts so dashboards can be tested against non-ideal data.
    """
    def __init__(self, device_ids: List[int]) -> None:
        """Seed per-device counters and baseline values for telemetry synthesis."""
        self.device_ids = device_ids
        self._seq_by_dev: Dict[int, int] = {d: 0 for d in device_ids}
        self._base_by_dev: Dict[int, float] = {d: 25.0 + d * 0.3 for d in device_ids}
        self._t0 = time.time()

    def _now_iso(self) -> str:
        """Return current UTC timestamp as ISO-8601 text."""
        return datetime.datetime.now(timezone.utc).isoformat()

    def generate_one(self, device_id: int) -> Optional[TemperatureSample]:
        """Generate one pseudo-random telemetry sample or dropout for a device."""
        # simulating Dropouts
        if random.random() < 0.01:
            return None

        t = time.time() - self._t0
        base = self._base_by_dev[device_id]

        # langsame Welle + kleines Rauschen + minimale Drift
        temp = base + 0.8 * math.sin(t / 15.0 + device_id) + random.gauss(0, 0.03)
        self._base_by_dev[device_id] += random.gauss(0, 0.0005)

        self._seq_by_dev[device_id] += 1
        return TemperatureSample(
            device_id=device_id,
            ts=self._now_iso(),
            temp_c=round(temp, 3),
            seq=self._seq_by_dev[device_id],
        )

source = MockPotentiostatSource(DEVICE_IDS)

# In-Memory latest cache
latest_by_dev: Dict[int, TemperatureSample] = {}

@app.get("/api/telemetry/temperature/latest")
def get_latest():
    """Return the latest cached telemetry sample per device.
    
    Parameters
    ----------
    None
        This callable does not receive explicit input parameters.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    for d in DEVICE_IDS:
        if d not in latest_by_dev:
            s = source.generate_one(d)
            if s:
                latest_by_dev[d] = s
    return {"samples": [asdict(latest_by_dev[d]) for d in DEVICE_IDS if d in latest_by_dev]}

def sse_format(event: str, data_obj, event_id: Optional[str] = None) -> str:
    """Serialize a server-sent-event message payload.
    
    Parameters
    ----------
    event : str
        Value supplied by the API caller or internal orchestration.
    data_obj : Any
        Value supplied by the API caller or internal orchestration.
    event_id : Optional[str]
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    str
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Used by FastAPI routes and background slot worker orchestration.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    msg = ""
    if event_id is not None:
        msg += f"id: {event_id}\n"
    msg += f"event: {event}\n"
    msg += "data: " + json.dumps(data_obj, separators=(",", ":")) + "\n\n"
    return msg

@app.get("/api/telemetry/temperature/stream")
async def temperature_stream(rate_hz: float = Query(2.0, ge=0.2, le=20.0)):
    """Stream simulated telemetry samples over SSE.
    
    Parameters
    ----------
    rate_hz : float
        Value supplied by the API caller or internal orchestration.
    
    Returns
    -------
    Any
        Value returned to the caller or consumed by the route handler.
    
    Notes
    -----
    Called by GUI adapter HTTP clients through the FastAPI router.
    
    Raises
    ------
    HTTPException
        Raises HTTPException when request data, auth, or storage resolution fails.
    """
    interval = 1.0 / rate_hz

    async def gen():
        """Yield SSE payloads for temperature samples and periodic ping keepalives."""
        last_ping = time.time()

        # optional initial burst
        for d in DEVICE_IDS:
            s = source.generate_one(d)
            if s:
                latest_by_dev[d] = s
                yield sse_format("temp", asdict(s), event_id=f"{d}:{s.seq}")

        while True:
            start = time.time()

            for d in DEVICE_IDS:
                s = source.generate_one(d)
                if s:
                    latest_by_dev[d] = s
                    yield sse_format("temp", asdict(s), event_id=f"{d}:{s.seq}")

            if time.time() - last_ping >= 15:
                last_ping = time.time()
                yield sse_format("ping", {"ts": datetime.datetime.now(timezone.utc).isoformat()})

            elapsed = time.time() - start
            await asyncio.sleep(max(0.0, interval - elapsed))

    return StreamingResponse(gen(), media_type="text/event-stream")
