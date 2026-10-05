"""Bridge between stored API connections and CLI MigrationContext.

Each background job gets an isolated working directory containing its own
``config.yaml`` (with tokens, mode 0600), state DB, and ``exports/`` /
``xformed/`` / ``schemas/`` / ``reports/`` / ``logs/`` trees. Workers use
absolute paths only and never chdir: the process working directory is shared
with concurrent sync endpoints, so a global chdir would corrupt sibling
requests.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:  # pragma: no cover
    from aap_migration.migration.state import MigrationState

from aap_migration.api.store import (
    SNAPSHOT_FERNET_FP,
    SNAPSHOT_FP,
    SNAPSHOT_NEED,
    SNAPSHOT_SOURCE_ID,
    SNAPSHOT_STABLE,
    SNAPSHOT_TARGET_ID,
    NeedScope,
    resolve_active_pair,
)
from aap_migration.cli.context import MigrationContext
from aap_migration.config import (
    AAPInstanceConfig,
    MigrationConfig,
    PathConfig,
    StateConfig,
)


def _instance_config(record: dict[str, Any]) -> AAPInstanceConfig:
    """Build an :class:`AAPInstanceConfig` from a stored connection record."""
    return AAPInstanceConfig(
        url=record["url"],
        token=record["token"],
        verify_ssl=record["verify_ssl"],
        timeout=record["timeout"],
    )


def build_migration_config(
    source: dict[str, Any],
    target: dict[str, Any],
    job_dir: str | Path,
) -> MigrationConfig:
    """Build a job-scoped MigrationConfig from stored connection records."""
    job_dir = Path(job_dir).resolve()
    return MigrationConfig(
        source=_instance_config(source),
        target=_instance_config(target),
        paths=PathConfig(
            base_dir=str(job_dir),
            export_dir=str(job_dir / "exports"),
            transform_dir=str(job_dir / "xformed"),
            schema_dir=str(job_dir / "schemas"),
            report_dir=str(job_dir / "reports"),
            backup_dir=str(job_dir / "backups"),
            mappings_file="config/mappings.yaml",
            ignored_endpoints_file="config/ignored_endpoints.yaml",
        ),
        state=StateConfig(db_path=str(job_dir / "migration_state.db")),
    )


def write_job_config(config: MigrationConfig, job_dir: str | Path) -> Path:
    """Persist a job config (including tokens) with mode 0600. Returns path."""
    job_dir = Path(job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    config_path = job_dir / "config.yaml"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        yaml.safe_dump(config.model_dump(mode="json"), fh, default_flow_style=False)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    return config_path


def build_job_context(
    job_dir: str | Path,
    source_id: str | None = None,
    target_id: str | None = None,
    db_path: str | None = None,
    need: NeedScope = "both",
) -> tuple[MigrationContext, MigrationConfig]:
    """Resolve connections and create an isolated job context + config file."""
    source, target = resolve_active_pair(source_id, target_id, db_path, need=need)
    if need != "source":
        assert target is not None  # need="both" (default) guarantees a target
    # Execution-time SSRF re-check (#20): the stored URL may have been
    # rebound (or edited) to a metadata endpoint since create/update
    # validation; the worker's fetches would deliver the bearer token there.
    # Bounded variant: DNS runs on a helper thread with a 10s cap so one
    # poison hostname fails one job fast instead of head-of-line blocking
    # the single FIFO worker on unbounded getaddrinfo.
    #
    # DNS-rebind TOCTOU note (#2): this execution-time check still leaves a
    # window before each fetch (rebind between here and the HTTP request).
    # The window is narrowed by double-verification: this check plus a
    # second pre-fetch re-verification inside BaseAPIClient.request() just
    # before the bearer token is sent (see base_client).
    from aap_migration.utils.ssrf import reverify_execution_url_bounded

    reverify_execution_url_bounded(source["url"])
    if target is not None:
        reverify_execution_url_bounded(target["url"])
    # Source-only jobs (need="source") never touch ctx.target_client (lazy);
    # reuse the source record as the config placeholder so MigrationConfig
    # (which requires a target) can still be built for the isolated job dir.
    config = build_migration_config(source, target if target is not None else source, job_dir)
    config_path = write_job_config(config, job_dir)
    # Point at repo-level mappings/ignored-endpoints files when present so
    # resource_mappings / ignored_endpoints behave like the CLI.
    try:
        from aap_migration.config import load_config_from_yaml

        config = load_config_from_yaml(config_path)
    except Exception:
        pass
    ctx = MigrationContext(config_path=config_path, log_level="ERROR")
    ctx._config = config
    return ctx, config


def build_ephemeral_context(
    source_id: str | None = None,
    target_id: str | None = None,
    db_path: str | None = None,
) -> MigrationContext:
    """Build an in-memory context for quick sync endpoints (no job dir)."""
    source, target = resolve_active_pair(source_id, target_id, db_path)
    assert target is not None  # need="both" (default) guarantees a target
    config = MigrationConfig(
        source=_instance_config(source),
        target=_instance_config(target),
    )
    ctx = MigrationContext(config_path=Path(".").resolve(), log_level="ERROR")
    ctx._config = config
    return ctx


def close_job_context(ctx: Any) -> None:
    """Best-effort close of per-job HTTP clients (never raises).

    Shared lifecycle home for worker teardown (services) and sync routers
    (config/connections): closes ``_source_client`` / ``_target_client``
    whether the close method is sync or async. Async closes are awaited on
    the owning loop when possible, run to completion on a helper thread when
    already inside a loop, and never fire-and-forget leaked.
    """
    import asyncio
    import threading

    for attr in ("_source_client", "_target_client"):
        client = getattr(ctx, attr, None)
        if client is None:
            continue
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if asyncio.iscoroutine(result):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
                else:
                    done = threading.Event()
                    errors: list[BaseException] = []

                    def _runner(
                        _result: Any = result,
                        _done: threading.Event = done,
                        _errors: list[BaseException] = errors,
                    ) -> None:
                        try:
                            asyncio.run(_result)
                        except BaseException as exc:  # noqa: BLE001 - teardown
                            _errors.append(exc)
                        finally:
                            _done.set()

                    thread = threading.Thread(target=_runner, daemon=True)
                    thread.start()
                    closed = done.wait(timeout=10)
                    if not closed:
                        import logging as _logging

                        _logging.getLogger("aap_migration.api.context").warning(
                            "async client close timed out after 10s for %s; "
                            "joining helper with a second 5s bound (pool may leak)",
                            attr,
                        )
                        thread.join(timeout=5)
                    else:
                        thread.join(timeout=5)
                    if errors:
                        import logging as _logging

                        _logging.getLogger("aap_migration.api.context").warning(
                            "async client close for %s raised %r (teardown degraded)",
                            attr,
                            errors[0],
                        )
        except Exception:
            pass


def setup_job_logging(job_dir: str | Path) -> None:
    """Attach a per-job file handler without touching global configuration.

    The previous implementation called the global ``configure_logging`` per
    job, which cleared all root handlers and misrouted concurrent sync-endpoint
    logs while leaking file descriptors. Now each job gets its own
    ``FileHandler`` on a job-scoped logger; global handlers are never removed.
    """
    import logging

    try:
        log_file = Path(job_dir).resolve() / "logs" / "api.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("aap_migration.api.job")
        for handler in logger.handlers:
            try:
                if getattr(handler, "_api_job_file", None) == str(log_file):
                    return
            except Exception:
                continue
        handler = logging.FileHandler(str(log_file))
        handler._api_job_file = str(log_file)  # type: ignore[attr-defined]
        logger.addHandler(handler)
    except Exception:
        pass


def teardown_job_logging(job_dir: str | Path) -> None:
    """Remove and close the per-job file handler (never raises).

    Shared teardown home for :func:`chained_ctx` callers in services: without
    this, every job leaks one open FD plus one fan-out handler on the
    process-wide ``aap_migration.api.job`` logger until FD exhaustion.
    """
    import logging

    try:
        log_file = str(Path(job_dir).resolve() / "logs" / "api.log")
        logger = logging.getLogger("aap_migration.api.job")
        for handler in list(logger.handlers):
            try:
                if getattr(handler, "_api_job_file", None) == log_file:
                    logger.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass


def default_state_db_path() -> str | None:
    """Resolve the server-default migration state DB (CLI parity).

    Checks ``MIGRATION_STATE_DB_PATH``, then ``./migration_state.db`` and
    ``./database/migration_state.db``. Returns None when no DB exists.
    Uses absolute paths so a worker thread can never hijack the lookup.
    """
    startup_cwd = Path(os.environ.get("AAP_BRIDGE_STARTUP_CWD", os.getcwd())).resolve()
    candidates = (
        os.environ.get("MIGRATION_STATE_DB_PATH"),
        str(startup_cwd / "migration_state.db"),
        str(startup_cwd / "database" / "migration_state.db"),
    )
    for candidate in candidates:
        if not candidate:
            continue
        fs_path = candidate
        for scheme in ("sqlite:///", "sqlite://"):
            if fs_path.startswith(scheme):
                fs_path = fs_path[len(scheme) :]
                break
        else:
            if "://" in fs_path:  # non-sqlite URL: assume usable
                return candidate
        abs_candidate = fs_path if os.path.isabs(fs_path) else str(startup_cwd / fs_path)
        if os.path.exists(abs_candidate):
            return abs_candidate
    return None


def open_state(db_path: str) -> MigrationState:
    """Open a MigrationState for an explicit DB path."""
    from aap_migration.config import StateConfig
    from aap_migration.migration.state import MigrationState

    return MigrationState(config=StateConfig(db_path=db_path))


@contextlib.contextmanager
def open_throwaway_state(
    prefix: str = "depcheck-state-",
) -> Generator[MigrationState, None, None]:
    """Yield an isolated throwaway MigrationState, auto-cleaned on exit.

    Single home for read-only dependency checks (validation + import
    depcheck): uses :class:`tempfile.TemporaryDirectory` so the /tmp tree
    is removed even when validation raises, mirroring the
    ``preview_transform`` rmtree-finally. Never touches the server-default
    DB path. Use as ``with open_throwaway_state() as state:``.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield open_state(os.path.join(tmp, "migration_state.db"))


def resolve_job_state(
    job_id: str | None,
    strict: bool = False,
    allow_statuses: tuple[str, ...] = (
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ),
) -> tuple[Path | None, MigrationState | None]:
    """Resolve ``(workdir, state)`` for a job-scoped state read (single home).

    Calls :func:`resolve_workdir` (chaining rules) then appends
    ``migration_state.db``. Maps ``ValueError`` to HTTP errors: unknown
    ids and missing workdirs are always 404; jobs outside
    *allow_statuses* are 409 (pass ``("succeeded",)`` to preserve the
    dependency-gate chaining rule). A missing state DB raises 404 when
    *strict* is true, otherwise returns ``(workdir, None)`` so callers
    emit the lenient 200+warning shape (or fall back to another scope).

    Without ``job_id`` resolves the server-default DB: ``(None, state)``
    or ``(None, None)`` (404 when *strict*). Raises 404 for unknown jobs.
    """
    from fastapi import HTTPException

    from aap_migration.api.jobs import get_job_manager

    if not job_id:
        state = open_default_state()
        if state is None and strict:
            raise HTTPException(status_code=404, detail="No migration state DB found")
        return None, state
    try:
        workdir = resolve_workdir(
            {"job_id": job_id},
            get_job_manager().base_dir,
            allow_statuses=allow_statuses,
        )
    except (KeyError, ValueError) as exc:
        # Single home for the mapping (P2 #20): UnknownJobError/KeyError
        # (incl. WorkdirGoneError) -> 404, ConflictError -> 409, any other
        # ValueError -> 400. Codes come from exception types only, never
        # message text.
        from aap_migration.api.routers._common import _store_http_error

        raise _store_http_error(exc) from exc
    candidate = workdir / "migration_state.db"
    if not candidate.exists():
        if strict:
            raise HTTPException(status_code=404, detail="No migration state DB found")
        return workdir, None
    return workdir, open_state(str(candidate))


def open_default_state() -> MigrationState | None:
    """Open the server-default migration state, or None if absent."""
    db_path = default_state_db_path()
    if db_path is None:
        return None
    return open_state(db_path)


def submit_pair_snapshot(
    source_id: str | None,
    target_id: str | None,
    need: NeedScope,
) -> dict[str, Any]:
    """Pin the effective pair for a connection-bearing job at submit time.

    Single home (next to :func:`check_pair_switch` /
    :func:`verify_execution_pair`) for the ``_snapshot_*`` write that used
    to live in ``routers._common.submit_chained``: execution re-resolves
    live rows, so without a submit-time fingerprint a token rotation, URL
    edit, or active-pair move between 202 and execution would silently
    switch credentials or retarget the run. Raises KeyError/ValueError
    like :func:`store.pair_fingerprint` so no job is enqueued without
    snapshot keys.
    """
    from aap_migration.api.store import pair_fingerprint

    snap = pair_fingerprint(source_id, target_id, need=need)
    try:
        from aap_migration.api.security import fernet_key_fingerprint

        fernet_fp: str | None = fernet_key_fingerprint()
    except Exception:
        fernet_fp = None
    return {
        SNAPSHOT_SOURCE_ID: snap["source_id"],
        SNAPSHOT_TARGET_ID: snap["target_id"],
        SNAPSHOT_FP: snap["fp"],
        SNAPSHOT_STABLE: snap.get("stable"),
        SNAPSHOT_NEED: need,
        SNAPSHOT_FERNET_FP: fernet_fp,
    }


def check_pair_switch(
    params: dict[str, Any],
    ref_params: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Single home for chained-pair-switch validation (see _common).

    Raises ValueError when the requesting params override the referenced
    job's connections without explicit ``allow_pair_switch=true``. When
    *snapshot* (resolved ids captured at submit) is present, any difference
    between the snapshot and the referenced job's resolved pair also requires
    opt-in, so implicit active-pair changes cannot silently switch pipelines.
    """
    allow = bool(params.get("allow_pair_switch", False))
    _snap_key = {"source_id": SNAPSHOT_SOURCE_ID, "target_id": SNAPSHOT_TARGET_ID}
    for key in ("source_id", "target_id"):
        new = params.get(key)
        if not new:
            continue
        # Compare against the referenced job's effective id (snapshot pin
        # falling back to the explicit id, mirroring the snapshot branch
        # below): passing the same effective pair explicitly must pass
        # like the all-fallback case instead of demanding opt-in.
        old = ref_params.get(_snap_key[key])
        if old is None:
            old = ref_params.get(key)
        if (old is None or new != old) and not allow:
            raise ValueError(
                f"Chained job overrides {key} ({old} -> {new}); "
                "pass allow_pair_switch=true to opt in explicitly."
            )
    if snapshot:
        _snap_key = {"source_id": SNAPSHOT_SOURCE_ID, "target_id": SNAPSHOT_TARGET_ID}
        for key in ("source_id", "target_id"):
            snap = snapshot.get(key)
            old = ref_params.get(_snap_key[key], ref_params.get(key))
            if snap is not None and old is not None and snap != old:
                if not allow:
                    raise ValueError(
                        f"Active {key} changed since the referenced job was "
                        f"submitted ({old} -> {snap}); pass "
                        "allow_pair_switch=true to opt in explicitly."
                    )


def check_tls_posture(params: dict[str, Any], need: NeedScope = "both") -> None:
    """Reject per-job verify_ssl=false that weakens stored posture (P1 #3).

    Per-job ``verify_ssl``/``timeout`` overrides (IAM schemas) resolve
    explicit-wins at execution, but the submit-time fingerprint covers
    stored rows only: a ``false`` override against a stored
    ``verify_ssl=true`` connection would silently run weaker TLS than the
    snapshot attests, with no drift error and no admin veto. Fail closed
    here (submit: 400; execution: job failure) instead. Strengthening
    (stored false -> override true) and timeout overrides stay allowed;
    connectionless jobs (``need="none"``) skip the check. Raises
    ValueError. Missing/deleted connections raise KeyError/ValueError
    and are left for the normal resolution path -- callers that already
    resolved (submit snapshot) see this only for genuine weakening.
    """
    override = params.get("verify_ssl")
    if override is not False or not need or need == "none":
        return
    from aap_migration.api.store import stored_posture

    try:
        posture = stored_posture(params.get("source_id"), params.get("target_id"), need=need)
    except (KeyError, ValueError):
        return
    weakened = [side for side, post in posture.items() if post.get("verify_ssl")]
    if weakened:
        raise ValueError(
            "Per-job verify_ssl=false would weaken the stored connection "
            f"posture ({', '.join(sorted(weakened))} stored verify_ssl=true); "
            "update the stored connection instead of overriding per job."
        )


def verify_execution_pair(params: dict[str, Any]) -> None:
    """Fail fast when connections drifted between submit (202) and execution.

    Compares the submit-time fingerprint (``_snapshot_*`` keys written by
    ``submit_chained``) against the currently stored pair. A token rotation,
    URL edit, connection delete, or active-pair move in the FIFO queue window
    fails the job with an operator-actionable error instead of silently
    running under credentials or targets the submitter never presented.
    Explicit ``allow_pair_switch=true`` opts into intentional *id* switches
    with an UNCHANGED fingerprint only: any fingerprint drift (credential
    rotation, URL edit, TLS/timeout posture change) fails even when the
    resolved ids also switched, so a rotation can never piggyback on a
    switched job -- every rotation requires resubmit. Kind mismatches
    still fail in resolution.
    Params without snapshot keys (pre-snapshot records, connectionless jobs)
    are returned unchecked.

    Operator note: rotate tokens/URLs (or move the active pair) only when
    the queue is drained -- every queued job pinned to the old pair fails
    here at execution and must be resubmitted, so a mid-queue rotation
    turns one admin action into N resubmits.
    """
    snap_src = params.get(SNAPSHOT_SOURCE_ID)
    snap_tgt = params.get(SNAPSHOT_TARGET_ID)
    snap_fp = params.get(SNAPSHOT_FP)
    if (
        snap_src is None
        and snap_tgt is None
        and snap_fp is None
        and params.get(SNAPSHOT_FERNET_FP) is None
    ):
        return
    # TLS posture veto (P1 #3, defense in depth behind the submit-time
    # check): params that bypassed submit (direct manager use, crafted
    # records) must not run weakened TLS either.
    need_pre = params.get(SNAPSHOT_NEED)
    if need_pre is None:
        need_pre = "both" if snap_tgt is not None else "source"
    if need_pre in ("both", "source", "none"):
        check_tls_posture(params, cast(NeedScope, need_pre))
    # Fernet key rotation guard (drain-before-rotate, enforced): the
    # encryption key is pinned at submit; a rotation while queued fails
    # fast here with one actionable error instead of N per-row decrypt
    # failures at worker time. Pre-fingerprint records skip the check.
    snap_fernet = params.get(SNAPSHOT_FERNET_FP)
    if snap_fernet is not None:
        try:
            from aap_migration.api.security import fernet_key_fingerprint

            current_fernet = fernet_key_fingerprint()
        except Exception:
            current_fernet = None
        if current_fernet is not None and current_fernet != snap_fernet:
            raise ValueError(
                "API encryption key (AAP_BRIDGE_API_KEY) changed since this job "
                "was submitted; drain the queue before rotating keys, then "
                "resubmit. Queued jobs pinned to the old key cannot decrypt "
                "under the new one."
            )
    from aap_migration.api.store import pair_fingerprint

    # Source-only jobs hash only the source side (see store.pair_fingerprint):
    # use the submit-time need so an active target configured while queued
    # does not fail them. Records predating _snapshot_need infer it from the
    # snapshot itself; any other value is corrupt params and fails closed.
    need = params.get(SNAPSHOT_NEED)
    if need is None:
        need = "both" if snap_tgt is not None else "source"
    if need not in ("both", "source", "none"):
        raise ValueError(
            f"Job connection scope is corrupt (snapshot need={need!r}); resubmit the job."
        )
    need = cast(NeedScope, need)
    try:
        current = pair_fingerprint(params.get("source_id"), params.get("target_id"), need=need)
    except KeyError as exc:
        raise ValueError(
            "A connection used by this job was deleted after submit; "
            "resubmit against the current connections."
        ) from exc
    except ValueError as exc:
        # pair_fingerprint surfaces operator-actionable rotation/decrypt
        # guidance (e.g. decrypt_token key-change message); preserve it so
        # queued jobs fail with the actionable text instead of a bare type.
        raise ValueError(str(exc)) from exc
    allow_switch = bool(params.get("allow_pair_switch", False))
    ids_changed = (snap_src is not None and current["source_id"] != snap_src) or (
        snap_tgt is not None and current["target_id"] != snap_tgt
    )
    fp_changed = snap_fp is not None and current["fp"] != snap_fp
    if allow_switch:
        # Opt-in covers intentional id switches with an unchanged
        # fingerprint only. Any fingerprint drift (rotation, URL edit,
        # posture change) fails closed and requires resubmit, even when
        # the ids also switched: a rotation must never piggyback on a
        # switched job.
        if fp_changed:
            raise ValueError(
                "Credentials or connection posture changed since this job "
                "was submitted (new fingerprint); resubmit to run under "
                "the current pair. allow_pair_switch covers id switches "
                "with an unchanged fingerprint, not rotations."
            )
        return
    if ids_changed or fp_changed:
        raise ValueError(
            "Connections changed since this job was submitted "
            f"(snapshot {snap_src}/{snap_tgt} vs current "
            f"{current['source_id']}/{current['target_id']}); resubmit to "
            "run under the current pair or pass allow_pair_switch=true to "
            "opt in explicitly."
        )


def resolve_workdir(
    params: dict[str, Any],
    job_dir: str | Path,
    *,
    allow_statuses: tuple[str, ...] = ("succeeded",),
) -> Path:
    """Resolve the working directory for a job, honoring ``job_id`` chaining.

    When ``params["job_id"]`` references an existing job, that job's directory
    (with its exports/xformed/state DB) is reused so ETL phases chain.
    Chaining onto jobs outside *allow_statuses* is rejected: running against
    partial outputs can report success over half-done work that rerunning
    does not repair. Raises ValueError for unknown or disallowed jobs.
    Resume operations pass ``allow_statuses=("succeeded", "failed",
    "cancelled")`` so failed jobs can be resumed.
    """
    from aap_migration.api.jobs import get_job_manager

    ref = params.get("job_id")
    if ref:
        try:
            existing = get_job_manager().get_internal(ref)
        except KeyError as exc:
            from aap_migration.api.jobs._records import UnknownJobError

            raise UnknownJobError(f"Unknown job_id '{ref}'") from exc
        status = existing.get("status")
        if status not in allow_statuses:
            # Lifecycle conflict (P2 #20: typed as ConflictError so the
            # shared type-based mapper yields 409, never a sniffed 400).
            from aap_migration.api.jobs._records import ConflictError

            raise ConflictError(
                f"Job '{ref}' is {status}; only {', '.join(allow_statuses)} jobs "
                "can be chained. Wait for it to succeed or omit job_id for a "
                "fresh directory."
            )
        workdir = Path(existing["job_dir"]).resolve()
        if not workdir.is_dir():
            from aap_migration.api.jobs._records import WorkdirGoneError

            raise WorkdirGoneError(f"Job '{ref}' working directory no longer exists")
        return workdir
    return Path(job_dir).resolve()


def _record_pair_lineage(params: dict[str, Any], workdir: Path, referenced: Any) -> None:
    """Record the producing-pair lineage for chained workdirs (never raises).

    Appends ``{"ts", "source_id", "target_id", "fp", "allow_pair_switch",
    "switched"}`` to ``<workdir>/.pair_lineage.jsonl`` whenever a chained
    job runs, so an export from pair A imported into pair B under
    ``allow_pair_switch=true`` leaves an auditable trail instead of silently
    reusing A's artifacts. A detected switch is also logged as a warning
    naming old -> new, making the mismatch explicit in server logs.
    """
    if referenced is None:
        return
    try:
        import json
        import logging
        from datetime import UTC, datetime

        snap_fp = params.get(SNAPSHOT_FP)
        snap_src = params.get(SNAPSHOT_SOURCE_ID)
        snap_tgt = params.get(SNAPSHOT_TARGET_ID)
        ref_params = referenced.get("params", {})
        old_fp = ref_params.get(SNAPSHOT_FP)
        switched = bool(
            snap_fp is not None
            and old_fp is not None
            and snap_fp != old_fp
            and bool(params.get("allow_pair_switch", False))
        )
        if switched:
            logging.getLogger("aap_migration.api.context").warning(
                "pair switch with opt-in: chained job reuses workdir %s from "
                "pair snapshot %s/%s and runs under %s/%s; artifacts now mix pairs",
                workdir,
                ref_params.get(SNAPSHOT_SOURCE_ID),
                ref_params.get(SNAPSHOT_TARGET_ID),
                snap_src,
                snap_tgt,
            )
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "source_id": snap_src,
            "target_id": snap_tgt,
            "fp": snap_fp,
            "allow_pair_switch": bool(params.get("allow_pair_switch", False)),
            "switched": switched,
        }
        with open(workdir / ".pair_lineage.jsonl", "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def setup_chained(
    params: dict[str, Any],
    job_dir: str | Path,
    *,
    allow_statuses: tuple[str, ...] = ("succeeded",),
    need: NeedScope = "both",
) -> tuple[MigrationContext, MigrationConfig, Path]:
    """Build (ctx, config, workdir) honoring ``job_id`` chaining.

    The job config is (re)written into the workdir so chained phases always
    run with the requesting connections. Switching AAP pairs mid-pipeline
    (export from pair A, import into pair B) requires explicit
    ``allow_pair_switch=true``; otherwise a ValueError names the mismatch.
    On a detected pair switch the chained phase runs in a FRESH workdir
    (sibling ``<parent>__switched_<fp8>`` with parent exports copied
    read-only) instead of rewriting config.yaml in place, so pair-A exports
    can never mix with pair-B writes. Resume callers pass
    ``allow_statuses`` including failed/cancelled.
    """
    from aap_migration.api.jobs import get_job_manager

    workdir = resolve_workdir(params, job_dir, allow_statuses=allow_statuses)
    ref = params.get("job_id")
    referenced: Any = None
    switched = False
    if ref:
        try:
            referenced = get_job_manager().get_internal(ref)
        except KeyError:
            referenced = None
        if referenced is not None:
            snapshot = {
                "source_id": params.get(SNAPSHOT_SOURCE_ID),
                "target_id": params.get(SNAPSHOT_TARGET_ID),
            }
            snapshot = {k: v for k, v in snapshot.items() if v is not None}
            check_pair_switch(
                params,
                referenced.get("params", {}),
                snapshot=snapshot or None,
            )
            # Detect opt-in pair switch for isolation (same predicate as
            # _record_pair_lineage): fresh workdir instead of in-place reuse.
            try:
                snap_fp = params.get(SNAPSHOT_FP)
                old_fp = referenced.get("params", {}).get(SNAPSHOT_FP)
                switched = bool(
                    snap_fp is not None
                    and old_fp is not None
                    and snap_fp != old_fp
                    and bool(params.get("allow_pair_switch", False))
                )
            except Exception:
                switched = False
    # Execution-time pinning (#3/#14): the submit-time fingerprint must still
    # match the stored pair; a rotation/edit/move in the queue window fails
    # here instead of running under un-presented credentials. IAM workers
    # call verify_execution_pair directly (they resolve without setup_chained).
    verify_execution_pair(params)
    if switched:
        import shutil
        import uuid

        parent = Path(workdir)
        fp_short = str(params.get(SNAPSHOT_FP) or "pair")[:8]
        fresh = parent.parent / f"{parent.name}__switched_{fp_short}_{uuid.uuid4().hex[:6]}"
        try:
            fresh.mkdir(parents=True, exist_ok=False)
            # Copy parent exports read-only for reference; never reuse the
            # parent dir for writes. Missing dirs are fine (fresh chain).
            for child in ("exports", "xformed", "schemas"):
                src = parent / child
                if src.is_dir():
                    shutil.copytree(src, fresh / child, dirs_exist_ok=True)
            # Carry lineage forward so audits see the fork.
            lineage = parent / ".pair_lineage.jsonl"
            if lineage.is_file():
                try:
                    shutil.copy2(lineage, fresh / ".pair_lineage.jsonl")
                except Exception:
                    pass
        except Exception as exc:
            # Fail open to parent reuse would reintroduce mixing; fail closed.
            raise ValueError(
                "Pair switch requires a fresh workdir but it could not be "
                "created; retry after checking job storage writability."
            ) from exc
        _record_pair_lineage(params, fresh, referenced if ref else None)
        ctx, config = build_job_context(
            fresh, params.get("source_id"), params.get("target_id"), need=need
        )
        setup_job_logging(fresh)
        return ctx, config, fresh
    _record_pair_lineage(params, workdir, referenced if ref else None)
    ctx, config = build_job_context(
        workdir, params.get("source_id"), params.get("target_id"), need=need
    )
    setup_job_logging(workdir)
    return ctx, config, workdir
