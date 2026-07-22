"""IAM permission analyser and migration engine.

Scans AAP 2.4 for the complete permission matrix (resource -> role ->
user/team), optionally migrates permissions to AAP 2.6, and produces
structured data for reporting.

Security design:
  - SSL verification ON by default (verify_ssl=True)
  - Tokens never logged, never written to reports
  - Pagination URLs validated against expected host (open-redirect defence)
  - State DB opened read-only (mode=ro)
  - All output files written with 0o600 permissions
  - Rate-limiting on every API call to prevent accidental DoS
  - Request timeouts enforced (default 60 s)
  - Query parameters passed via params dict, never interpolated into URLs
  - JSON responses parsed safely with explicit error handling
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from aap_migration.iam.exceptions import AuthenticationError, PaginationError
from aap_migration.iam.models import (
    CrossOrgShare,
    IAMAuditResult,
    IAMCheckpoint,
    MigrationStats,
    OrgSummary,
    PermissionEntry,
    SystemRoleEntry,
    TeamMembership,
)

logger = logging.getLogger(__name__)

RESOURCE_TYPES = [
    "organizations",
    "teams",
    "credentials",
    "projects",
    "inventories",
    "job_templates",
    "workflow_job_templates",
    "notification_templates",
    "instance_groups",
]

SINGULAR_TO_PLURAL: dict[str, str] = {
    "credential": "credentials",
    "project": "projects",
    "inventory": "inventories",
    "job_template": "job_templates",
    "workflow_job_template": "workflow_job_templates",
    "notification_template": "notification_templates",
    "instance_group": "instance_groups",
    "organization": "organizations",
    "team": "teams",
}

ROLE_NAME_MAP: dict[str, str] = {}

_TOKEN_VISIBLE_CHARS = 4


def _mask_sensitive(value: str) -> str:
    if not value or len(value) <= _TOKEN_VISIBLE_CHARS:
        return "****"
    return "****" + value[-_TOKEN_VISIBLE_CHARS:]


def _validate_api_url(url: str, label: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(
            f"{label} must use https:// or http:// (got {parsed.scheme!r})"
        )
    if not parsed.hostname:
        raise ValueError(f"{label} has no hostname")
    return url


class IAMAnalyser:
    """Scans and optionally migrates IAM permissions between AAP instances.

    Modes:
        audit()   — read-only scan of source; produces permission matrix
        migrate() — scan source, then apply permissions to target
    """

    def __init__(
        self,
        source_url: str,
        source_token: str,
        target_url: str | None = None,
        target_token: str | None = None,
        state_db_path: str | None = None,
        verify_ssl: bool = True,
        request_timeout: int = 60,
        rate_limit_delay: float = 0.15,
        max_workers: int = 1,
        scan_strategy: str = "resource",
        checkpoint_path: str | None = None,
        resume: bool = False,
        progress_callback: Callable[[str], None] | None = None,
    ):
        if scan_strategy not in ("resource", "principal"):
            raise ValueError(
                f"scan_strategy must be 'resource' or 'principal', "
                f"got '{scan_strategy}'"
            )
        self.scan_strategy = scan_strategy
        self._checkpoint_path = checkpoint_path
        self._resume = resume
        self._checkpoint: IAMCheckpoint | None = None

        self.source_url = _validate_api_url(source_url, "Source URL")
        self.source_token = source_token
        if not source_token:
            raise ValueError("Source token must not be empty")

        self.target_url: str | None = None
        self.target_token: str | None = None
        if target_url:
            self.target_url = _validate_api_url(target_url, "Target URL")
            if "/api/controller/v2" not in self.target_url:
                logger.warning(
                    "Target URL may be incorrect for AAP 2.6 — "
                    "expected '/api/controller/v2' in path: %s",
                    self.target_url,
                )
            self.target_token = target_token

        self.verify_ssl = verify_ssl
        self.request_timeout = request_timeout
        self.rate_limit_delay = rate_limit_delay
        self.max_workers = max(1, max_workers)
        self._progress = progress_callback or (lambda msg: None)

        pool_size = max(10, self.max_workers + 4)
        self._source_session = self._create_session(pool_size=pool_size)
        self._target_session = (
            self._create_session(pool_size=pool_size) if self.target_url else None
        )

        self._id_mappings: dict[str, dict[int, int]] = {}
        if state_db_path:
            self._load_id_mappings(state_db_path)

        self._org_cache: dict[int, str] = {}
        self._org_cache_lock = threading.Lock()
        self._source_host = urlparse(self.source_url).hostname
        self._target_host = (
            urlparse(self.target_url).hostname if self.target_url else None
        )

    def close(self) -> None:
        self._source_session.close()
        if self._target_session:
            self._target_session.close()

    def __enter__(self) -> IAMAnalyser:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Session / HTTP helpers ────────────────────────────────────────

    @staticmethod
    def _create_session(pool_size: int = 10) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=pool_size)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {"User-Agent": "aap-bridge-iam/1.0", "Accept": "application/json"}
        )
        return session

    @staticmethod
    def _safe_json(resp: requests.Response) -> dict | None:
        if not resp.text:
            return None
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            logger.warning(
                "Invalid JSON from %s (HTTP %d)", resp.url, resp.status_code
            )
            return None

    def _validate_next_url(
        self, next_url: str | None, expected_host: str | None
    ) -> str | None:
        if not next_url:
            return None
        if not next_url.startswith("http"):
            return None
        parsed = urlparse(next_url)
        if parsed.hostname != expected_host:
            logger.warning(
                "Pagination URL redirects to unexpected host %s "
                "(expected %s) — skipping",
                parsed.hostname,
                expected_host,
            )
            return None
        return next_url

    _PAGINATE_MAX_RETRIES = 3
    _PAGINATE_BACKOFF_BASE = 1.0  # seconds; doubles each retry

    @staticmethod
    def _retry_delay(resp: requests.Response, attempt: int, backoff_base: float) -> float:
        """Return seconds to wait before retrying. Honors Retry-After."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except (ValueError, TypeError):
                pass
        return backoff_base * (2 ** attempt)

    def _paginate(
        self,
        base_url: str,
        token: str,
        session: requests.Session,
        endpoint: str,
        expected_host: str | None,
        params: dict | None = None,
    ) -> list[dict]:
        results: list[dict] = []
        url: str | None = f"{base_url}/{endpoint.lstrip('/')}"
        initial_params: dict[str, Any] = {"page_size": 200}
        if params:
            initial_params.update(params)
        is_first_page = True
        expected_count: int | None = None
        parsed_base = urlparse(base_url)
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        while url:
            try:
                resp: requests.Response | None = None
                for attempt in range(self._PAGINATE_MAX_RETRIES):
                    resp = session.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        params=initial_params if is_first_page else None,
                        verify=self.verify_ssl,
                        timeout=self.request_timeout,
                    )
                    if resp.status_code == 200:
                        break
                    retryable = (
                        resp.status_code == 429
                        or resp.status_code >= 500
                    )
                    if not retryable:
                        break
                    if attempt < self._PAGINATE_MAX_RETRIES - 1:
                        delay = self._retry_delay(
                            resp, attempt, self._PAGINATE_BACKOFF_BASE,
                        )
                        logger.warning(
                            "Paginate %s returned HTTP %d, "
                            "retrying in %.1fs (attempt %d/%d)",
                            endpoint,
                            resp.status_code,
                            delay,
                            attempt + 1,
                            self._PAGINATE_MAX_RETRIES,
                        )
                        time.sleep(delay)

                assert resp is not None
                if resp.status_code != 200:
                    if is_first_page:
                        logger.warning(
                            "Paginate %s returned HTTP %d",
                            endpoint,
                            resp.status_code,
                        )
                        break
                    raise PaginationError(
                        endpoint,
                        "non-200 response while following next link",
                        url=url,
                        status_code=resp.status_code,
                        items_collected=len(results),
                        expected_count=expected_count,
                    )

                is_first_page = False

                data = self._safe_json(resp)
                if data is None:
                    break

                if expected_count is None and "count" in data:
                    expected_count = data["count"]

                results.extend(data.get("results", []))

                raw_next = data.get("next")
                if raw_next:
                    if raw_next.startswith("http"):
                        url = self._validate_next_url(raw_next, expected_host)
                    else:
                        url = f"{base_origin}{raw_next}"
                else:
                    url = None

                if url:
                    time.sleep(self.rate_limit_delay)

            except PaginationError:
                raise
            except requests.RequestException as exc:
                logger.error("Pagination error for %s: %s", endpoint, exc)
                break

        if expected_count is not None and len(results) != expected_count:
            raise PaginationError(
                endpoint,
                f"count mismatch: collected {len(results)}, "
                f"server reported {expected_count}",
                items_collected=len(results),
                expected_count=expected_count,
            )

        return results

    def _source_get(
        self, endpoint: str, params: dict | None = None
    ) -> dict | None:
        url = f"{self.source_url}/{endpoint.lstrip('/')}"
        try:
            resp = self._source_session.get(
                url,
                headers={"Authorization": f"Bearer {self.source_token}"},
                params=params,
                verify=self.verify_ssl,
                timeout=self.request_timeout,
            )
            if resp.status_code == 200:
                return self._safe_json(resp)
            logger.debug(
                "Source GET %s returned HTTP %d", endpoint, resp.status_code
            )
        except requests.RequestException as exc:
            logger.error("Source GET %s failed: %s", endpoint, exc)
        return None

    def _source_paginate(
        self, endpoint: str, params: dict | None = None
    ) -> list[dict]:
        return self._paginate(
            self.source_url,
            self.source_token,
            self._source_session,
            endpoint,
            self._source_host,
            params,
        )

    def _target_get(
        self, endpoint: str, params: dict | None = None
    ) -> dict | None:
        if not self.target_url or not self.target_token or not self._target_session:
            return None
        url = f"{self.target_url}/{endpoint.lstrip('/')}"
        try:
            resp = self._target_session.get(
                url,
                headers={"Authorization": f"Bearer {self.target_token}"},
                params=params,
                verify=self.verify_ssl,
                timeout=self.request_timeout,
            )
            if resp.status_code == 200:
                return self._safe_json(resp)
            logger.debug(
                "Target GET %s returned HTTP %d", endpoint, resp.status_code
            )
        except requests.RequestException as exc:
            logger.error("Target GET %s failed: %s", endpoint, exc)
        return None

    def _target_paginate(
        self, endpoint: str, params: dict | None = None
    ) -> list[dict]:
        if not self.target_url or not self.target_token or not self._target_session:
            raise RuntimeError("Target not configured")
        return self._paginate(
            self.target_url,
            self.target_token,
            self._target_session,
            endpoint,
            self._target_host,
            params,
        )

    def _target_post(
        self, endpoint: str, data: dict
    ) -> requests.Response | None:
        if not self.target_url or not self.target_token or not self._target_session:
            return None
        url = f"{self.target_url}/{endpoint.lstrip('/')}"
        try:
            return self._target_session.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.target_token}",
                    "Content-Type": "application/json",
                },
                json=data,
                verify=self.verify_ssl,
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            logger.error("Target POST %s failed: %s", endpoint, exc)
        return None

    # ── ID resolution ─────────────────────────────────────────────────

    def _load_id_mappings(self, db_path: str) -> None:
        if db_path.startswith("sqlite:///"):
            db_path = db_path[len("sqlite:///"):]

        if not os.path.exists(db_path):
            logger.info(
                "State DB not found at %s — using name-based resolution",
                db_path,
            )
            return
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT resource_type, source_id, target_id "
                "FROM id_mappings WHERE target_id IS NOT NULL"
            )
            for resource_type, source_id, target_id in cursor.fetchall():
                self._id_mappings.setdefault(resource_type, {})[
                    source_id
                ] = target_id
            conn.close()

            total = sum(len(m) for m in self._id_mappings.values())
            logger.info("Loaded %d ID mappings from state DB", total)
            self._progress(f"Loaded {total} ID mappings from state database")
        except (sqlite3.Error, OSError) as exc:
            logger.error("Failed to load ID mappings: %s", exc)

    def _get_target_id(
        self, resource_type: str, source_id: int
    ) -> int | None:
        return self._id_mappings.get(resource_type, {}).get(source_id)

    def _discover_target_id_by_name(
        self, resource_type: str, name: str
    ) -> int | None:
        param = "username" if resource_type == "users" else "name"
        data = self._target_get(
            f"{resource_type}/", params={param: name, "page_size": 1}
        )
        if data:
            results = data.get("results", [])
            if results:
                return results[0].get("id")
        return None

    def _get_org_name(self, org_id: int | None) -> str:
        if not org_id:
            return "N/A"
        with self._org_cache_lock:
            if org_id in self._org_cache:
                return self._org_cache[org_id]
        data = self._source_get(f"organizations/{org_id}/")
        name = data.get("name", f"org-{org_id}") if data else f"org-{org_id}"
        with self._org_cache_lock:
            self._org_cache[org_id] = name
        return name

    @staticmethod
    def _map_role_name(source_role: str) -> str:
        mapped = ROLE_NAME_MAP.get(source_role, source_role)
        if mapped != source_role:
            logger.debug("Mapped role name: %s -> %s", source_role, mapped)
        return mapped

    def _build_resource_org_map(
        self,
    ) -> tuple[dict[tuple[str, int], str], int]:
        """Pre-build (resource_type, resource_id) -> org_name map.

        Returns the map and total resources_scanned count.
        """
        org_map: dict[tuple[str, int], str] = {}
        resources_scanned = 0

        for resource_type in RESOURCE_TYPES:
            self._progress(f"  Building org map: {resource_type}...")
            resources = self._source_paginate(f"{resource_type}/")
            self._progress(
                f"    {len(resources)} {resource_type}"
            )

            for resource in resources:
                resources_scanned += 1
                res_id = resource["id"]
                org_id = resource.get("organization") or (
                    resource.get("summary_fields", {})
                    .get("organization", {})
                    .get("id")
                )
                org_map[(resource_type, res_id)] = self._get_org_name(
                    org_id
                )

        self._progress(
            f"  Org map complete: {resources_scanned} resources"
        )
        return org_map, resources_scanned

    # ── Phase 1: Scan permissions ─────────────────────────────────────

    def _fetch_role_members(
        self,
        role_id: int,
        role_name: str,
        resource_type: str,
        res_id: int,
        res_name: str,
        res_org: str,
    ) -> list[PermissionEntry]:
        """Fetch users and teams for a single role. Thread-safe."""
        results: list[PermissionEntry] = []

        for user in self._source_paginate(f"roles/{role_id}/users/"):
            user_org_id = (
                user.get("summary_fields", {})
                .get("organization", {})
                .get("id")
            )
            user_org = self._get_org_name(user_org_id)
            results.append(
                PermissionEntry(
                    resource_type=resource_type,
                    resource_id=res_id,
                    resource_name=res_name,
                    resource_org=res_org,
                    role_name=role_name,
                    principal_type="user",
                    principal_id=user["id"],
                    principal_name=user.get(
                        "username", f"user-{user['id']}"
                    ),
                    principal_org=user_org,
                    is_cross_org=(
                        user_org != "N/A"
                        and res_org != "N/A"
                        and user_org != res_org
                    ),
                )
            )

        for team in self._source_paginate(f"roles/{role_id}/teams/"):
            team_org_id = team.get("organization") or (
                team.get("summary_fields", {})
                .get("organization", {})
                .get("id")
            )
            team_org = self._get_org_name(team_org_id)
            results.append(
                PermissionEntry(
                    resource_type=resource_type,
                    resource_id=res_id,
                    resource_name=res_name,
                    resource_org=res_org,
                    role_name=role_name,
                    principal_type="team",
                    principal_id=team["id"],
                    principal_name=team.get(
                        "name", f"team-{team['id']}"
                    ),
                    principal_org=team_org,
                    is_cross_org=(
                        team_org != "N/A"
                        and res_org != "N/A"
                        and team_org != res_org
                    ),
                )
            )

        return results

    # ── Checkpoint persistence ────────────────────────────────────────

    def _save_checkpoint(
        self,
        entries: list[PermissionEntry],
        stats: MigrationStats,
        *,
        completed_resource_types: list[str] | None = None,
        completed_user_ids: list[int] | None = None,
        completed_team_ids: list[int] | None = None,
    ) -> None:
        """Atomic write of checkpoint state to disk (temp + rename, 0o600)."""
        if not self._checkpoint_path:
            return

        now = datetime.now(timezone.utc).isoformat()
        if self._checkpoint is None:
            self._checkpoint = IAMCheckpoint(
                scan_strategy=self.scan_strategy,
                source_url=self.source_url,
                started_at=now,
            )

        self._checkpoint.updated_at = now
        self._checkpoint.permissions = [e.to_dict() for e in entries]
        self._checkpoint.resources_scanned = stats.resources_scanned
        self._checkpoint.permissions_found = stats.permissions_found
        self._checkpoint.permissions_deduplicated = stats.permissions_deduplicated

        if completed_resource_types is not None:
            self._checkpoint.completed_resource_types = list(
                completed_resource_types
            )
        if completed_user_ids is not None:
            self._checkpoint.completed_user_ids = list(completed_user_ids)
        if completed_team_ids is not None:
            self._checkpoint.completed_team_ids = list(completed_team_ids)

        data = json.dumps(self._checkpoint.to_dict(), indent=2)
        dir_path = os.path.dirname(os.path.abspath(self._checkpoint_path))
        os.makedirs(dir_path, mode=0o700, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=dir_path, prefix=".iam_checkpoint_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._checkpoint_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.debug("Checkpoint saved: %s", self._checkpoint_path)

    def _load_checkpoint(self) -> IAMCheckpoint | None:
        """Load checkpoint from disk. Returns None if missing or invalid."""
        if not self._checkpoint_path or not os.path.exists(
            self._checkpoint_path
        ):
            return None

        try:
            with open(self._checkpoint_path) as f:
                data = json.load(f)
            checkpoint = IAMCheckpoint.from_dict(data)
            return checkpoint
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "Ignoring corrupt checkpoint %s: %s",
                self._checkpoint_path,
                exc,
            )
            return None

    def _validate_checkpoint(
        self, checkpoint: IAMCheckpoint
    ) -> str | None:
        """Validate checkpoint matches current run. Returns error or None."""
        if checkpoint.version != 1:
            return (
                f"Unsupported checkpoint version {checkpoint.version} "
                f"(expected 1)"
            )
        if checkpoint.scan_strategy != self.scan_strategy:
            return (
                f"Checkpoint strategy '{checkpoint.scan_strategy}' "
                f"does not match current '{self.scan_strategy}'"
            )
        if checkpoint.source_url != self.source_url:
            return (
                f"Checkpoint source '{checkpoint.source_url}' "
                f"does not match current '{self.source_url}'"
            )
        return None

    def scan_permissions(self) -> tuple[list[PermissionEntry], MigrationStats]:
        parallel = self.max_workers > 1
        mode_label = f"{self.max_workers} workers" if parallel else "sequential"
        self._progress(f"Phase 1: Scanning resource permissions ({mode_label})...")
        stats = MigrationStats()
        entries: list[PermissionEntry] = []
        seen: set[tuple] = set()
        completed_types: set[str] = set()

        if self._resume:
            checkpoint = self._load_checkpoint()
            if checkpoint:
                err = self._validate_checkpoint(checkpoint)
                if err:
                    self._progress(f"  Checkpoint invalid: {err} — starting fresh")
                else:
                    completed_types = set(checkpoint.completed_resource_types)
                    entries = [
                        PermissionEntry.from_dict(p)
                        for p in checkpoint.permissions
                    ]
                    seen = {e.dedup_key for e in entries}
                    stats.resources_scanned = checkpoint.resources_scanned
                    stats.permissions_found = checkpoint.permissions_found
                    stats.permissions_deduplicated = (
                        checkpoint.permissions_deduplicated
                    )
                    self._checkpoint = checkpoint
                    self._progress(
                        f"  Resumed from checkpoint: "
                        f"{len(completed_types)} resource types done, "
                        f"{len(entries)} permissions restored"
                    )

        for resource_type in RESOURCE_TYPES:
            if resource_type in completed_types:
                self._progress(f"  Skipping {resource_type} (already checkpointed)")
                continue
            self._progress(f"  Scanning {resource_type}...")
            resources = self._source_paginate(f"{resource_type}/")
            self._progress(f"  Found {len(resources)} {resource_type}")

            role_work: list[tuple[int, str, str, int, str, str]] = []

            for resource in resources:
                stats.resources_scanned += 1
                res_id = resource["id"]
                res_name = resource.get(
                    "name", resource.get("username", f"id-{res_id}")
                )

                org_id = resource.get("organization") or (
                    resource.get("summary_fields", {})
                    .get("organization", {})
                    .get("id")
                )
                res_org = self._get_org_name(org_id)

                object_roles = self._source_paginate(
                    f"{resource_type}/{res_id}/object_roles/"
                )

                for role in object_roles:
                    role_work.append((
                        role["id"],
                        role.get("name", ""),
                        resource_type,
                        res_id,
                        res_name,
                        res_org,
                    ))

            if not role_work:
                self._progress(f"  {resource_type}: 0 permission entries")
                completed_types.add(resource_type)
                self._save_checkpoint(
                    entries,
                    stats,
                    completed_resource_types=list(completed_types),
                )
                continue

            self._progress(
                f"  Fetching membership for {len(role_work)} roles..."
            )

            if parallel:
                completed = 0
                completed_lock = threading.Lock()

                def _progress_tick() -> None:
                    nonlocal completed
                    with completed_lock:
                        completed += 1
                        c = completed
                    if c % 500 == 0 or c == len(role_work):
                        self._progress(
                            f"    {c}/{len(role_work)} roles processed"
                        )

                with ThreadPoolExecutor(
                    max_workers=self.max_workers
                ) as executor:
                    futures = {
                        executor.submit(
                            self._fetch_role_members, *work_item
                        ): work_item
                        for work_item in role_work
                    }

                    for future in as_completed(futures):
                        try:
                            role_entries = future.result()
                        except Exception:
                            work = futures[future]
                            logger.error(
                                "Role membership fetch failed for role %d",
                                work[0],
                            )
                            _progress_tick()
                            continue

                        for entry in role_entries:
                            if entry.dedup_key not in seen:
                                seen.add(entry.dedup_key)
                                entries.append(entry)
                                stats.permissions_found += 1
                            else:
                                stats.permissions_deduplicated += 1
                        _progress_tick()
            else:
                for i, work_item in enumerate(role_work):
                    try:
                        role_entries = self._fetch_role_members(*work_item)
                    except Exception:
                        logger.error(
                            "Role membership fetch failed for role %d",
                            work_item[0],
                        )
                        continue

                    for entry in role_entries:
                        if entry.dedup_key not in seen:
                            seen.add(entry.dedup_key)
                            entries.append(entry)
                            stats.permissions_found += 1
                        else:
                            stats.permissions_deduplicated += 1

                    if (i + 1) % 500 == 0:
                        self._progress(
                            f"    {i + 1}/{len(role_work)} roles processed"
                        )

                    time.sleep(self.rate_limit_delay)

            type_count = sum(
                1 for e in entries if e.resource_type == resource_type
            )
            self._progress(
                f"  {resource_type}: {type_count} permission entries"
            )

            completed_types.add(resource_type)
            self._save_checkpoint(
                entries,
                stats,
                completed_resource_types=list(completed_types),
            )

        if stats.permissions_deduplicated:
            self._progress(
                f"Deduplicated {stats.permissions_deduplicated} duplicate entries"
            )
        self._progress(f"Total unique permissions: {stats.permissions_found}")
        return entries, stats

    # ── Phase 1-alt: Principal-side scan ─────────────────────────────

    def _fetch_principal_roles(
        self,
        principal_type: str,
        principal_id: int,
        principal_name: str,
        principal_org: str,
        org_map: dict[tuple[str, int], str],
    ) -> list[PermissionEntry]:
        """Fetch all role assignments for a single user or team."""
        results: list[PermissionEntry] = []
        endpoint = (
            f"users/{principal_id}/roles/"
            if principal_type == "user"
            else f"teams/{principal_id}/roles/"
        )

        roles = self._source_paginate(endpoint)
        for role in roles:
            sf = role.get("summary_fields", {})
            raw_type = sf.get("resource_type")
            if raw_type is None:
                continue

            resource_type = SINGULAR_TO_PLURAL.get(raw_type)
            if resource_type is None:
                logger.warning(
                    "Unknown resource_type '%s' in %s — skipping",
                    raw_type,
                    endpoint,
                )
                continue

            res_id = sf.get("resource_id")
            if res_id is None:
                continue

            res_name = sf.get("resource_name", f"id-{res_id}")
            res_org = org_map.get((resource_type, res_id), "N/A")

            results.append(
                PermissionEntry(
                    resource_type=resource_type,
                    resource_id=res_id,
                    resource_name=res_name,
                    resource_org=res_org,
                    role_name=role.get("name", ""),
                    principal_type=principal_type,
                    principal_id=principal_id,
                    principal_name=principal_name,
                    principal_org=principal_org,
                    is_cross_org=(
                        principal_org != "N/A"
                        and res_org != "N/A"
                        and principal_org != res_org
                    ),
                )
            )

        return results

    def scan_permissions_principal(
        self,
    ) -> tuple[list[PermissionEntry], MigrationStats]:
        """Scan permissions by enumerating principals instead of resources.

        For each user and team, fetches their direct role assignments via
        users/{id}/roles/ and teams/{id}/roles/. Produces the same
        PermissionEntry set as scan_permissions() but with far fewer API
        calls on environments where users+teams << resources*roles.
        """
        parallel = self.max_workers > 1
        mode_label = (
            f"{self.max_workers} workers" if parallel else "sequential"
        )
        self._progress(
            f"Phase 1: Scanning permissions — principal strategy "
            f"({mode_label})..."
        )

        stats = MigrationStats()
        entries: list[PermissionEntry] = []
        seen: set[tuple] = set()
        completed_user_ids: set[int] = set()
        completed_team_ids: set[int] = set()

        if self._resume:
            checkpoint = self._load_checkpoint()
            if checkpoint:
                err = self._validate_checkpoint(checkpoint)
                if err:
                    self._progress(
                        f"  Checkpoint invalid: {err} — starting fresh"
                    )
                else:
                    completed_user_ids = set(checkpoint.completed_user_ids)
                    completed_team_ids = set(checkpoint.completed_team_ids)
                    entries = [
                        PermissionEntry.from_dict(p)
                        for p in checkpoint.permissions
                    ]
                    seen = {e.dedup_key for e in entries}
                    stats.resources_scanned = checkpoint.resources_scanned
                    stats.permissions_found = checkpoint.permissions_found
                    stats.permissions_deduplicated = (
                        checkpoint.permissions_deduplicated
                    )
                    self._checkpoint = checkpoint
                    self._progress(
                        f"  Resumed from checkpoint: "
                        f"{len(completed_user_ids)} users + "
                        f"{len(completed_team_ids)} teams done, "
                        f"{len(entries)} permissions restored"
                    )

        org_map, resources_scanned = self._build_resource_org_map()
        if not self._resume or not completed_user_ids:
            stats.resources_scanned = resources_scanned

        # ── Scan users ──
        self._progress("  Scanning user role assignments...")
        users = self._source_paginate("users/")
        self._progress(f"  Found {len(users)} users")

        user_work: list[tuple[str, int, str, str]] = []
        for user in users:
            uid = user["id"]
            if uid in completed_user_ids:
                continue
            uname = user.get("username", f"user-{uid}")
            org_id = (
                user.get("summary_fields", {})
                .get("organization", {})
                .get("id")
            )
            user_org = self._get_org_name(org_id)
            user_work.append(("user", uid, uname, user_org))

        # ── Scan teams ──
        self._progress("  Scanning team role assignments...")
        teams = self._source_paginate("teams/")
        self._progress(f"  Found {len(teams)} teams")

        team_work: list[tuple[str, int, str, str]] = []
        for team in teams:
            tid = team["id"]
            if tid in completed_team_ids:
                continue
            tname = team.get("name", f"team-{tid}")
            org_id = team.get("organization") or (
                team.get("summary_fields", {})
                .get("organization", {})
                .get("id")
            )
            team_org = self._get_org_name(org_id)
            team_work.append(("team", tid, tname, team_org))

        if completed_user_ids or completed_team_ids:
            self._progress(
                f"  Skipped {len(completed_user_ids)} users + "
                f"{len(completed_team_ids)} teams (already checkpointed)"
            )

        all_work = user_work + team_work
        self._progress(
            f"  Fetching roles for {len(user_work)} users + "
            f"{len(team_work)} teams..."
        )

        def _process_principal(
            work_item: tuple[str, int, str, str],
        ) -> list[PermissionEntry]:
            ptype, pid, pname, porg = work_item
            return self._fetch_principal_roles(
                ptype, pid, pname, porg, org_map,
            )

        if parallel:
            completed = 0
            completed_lock = threading.Lock()

            def _progress_tick() -> None:
                nonlocal completed
                with completed_lock:
                    completed += 1
                    c = completed
                if c % 500 == 0 or c == len(all_work):
                    self._progress(
                        f"    {c}/{len(all_work)} principals processed"
                    )

            with ThreadPoolExecutor(
                max_workers=self.max_workers
            ) as executor:
                futures = {
                    executor.submit(
                        _process_principal, work_item
                    ): work_item
                    for work_item in all_work
                }

                for future in as_completed(futures):
                    work = futures[future]
                    ptype, pid = work[0], work[1]
                    try:
                        role_entries = future.result()
                    except Exception:
                        logger.error(
                            "Principal role fetch failed for %s %d",
                            ptype,
                            pid,
                        )
                        _progress_tick()
                        continue

                    for entry in role_entries:
                        if entry.dedup_key not in seen:
                            seen.add(entry.dedup_key)
                            entries.append(entry)
                            stats.permissions_found += 1
                        else:
                            stats.permissions_deduplicated += 1

                    if ptype == "user":
                        completed_user_ids.add(pid)
                    else:
                        completed_team_ids.add(pid)

                    _progress_tick()

                    if completed % 100 == 0:
                        self._save_checkpoint(
                            entries,
                            stats,
                            completed_user_ids=list(completed_user_ids),
                            completed_team_ids=list(completed_team_ids),
                        )
        else:
            for i, work_item in enumerate(all_work):
                ptype, pid = work_item[0], work_item[1]
                try:
                    role_entries = _process_principal(work_item)
                except Exception:
                    logger.error(
                        "Principal role fetch failed for %s %d",
                        ptype,
                        pid,
                    )
                    continue

                for entry in role_entries:
                    if entry.dedup_key not in seen:
                        seen.add(entry.dedup_key)
                        entries.append(entry)
                        stats.permissions_found += 1
                    else:
                        stats.permissions_deduplicated += 1

                if ptype == "user":
                    completed_user_ids.add(pid)
                else:
                    completed_team_ids.add(pid)

                if (i + 1) % 100 == 0:
                    self._save_checkpoint(
                        entries,
                        stats,
                        completed_user_ids=list(completed_user_ids),
                        completed_team_ids=list(completed_team_ids),
                    )

                if (i + 1) % 500 == 0:
                    self._progress(
                        f"    {i + 1}/{len(all_work)} principals "
                        f"processed"
                    )

                time.sleep(self.rate_limit_delay)

        # Every team implicitly has Read on itself. teams/{id}/roles/
        # does not return this, but roles/{read_id}/teams/ does — so
        # the resource-side scanner captures it. Add them here for
        # equivalence.
        for _pt, tid, tname, torg in team_work:
            key = ("teams", tid, "Read", "team", tid)
            if key not in seen:
                seen.add(key)
                entries.append(
                    PermissionEntry(
                        resource_type="teams",
                        resource_id=tid,
                        resource_name=tname,
                        resource_org=torg,
                        role_name="Read",
                        principal_type="team",
                        principal_id=tid,
                        principal_name=tname,
                        principal_org=torg,
                        is_cross_org=False,
                    )
                )
                stats.permissions_found += 1

        user_count = sum(
            1 for e in entries if e.principal_type == "user"
        )
        team_count = sum(
            1 for e in entries if e.principal_type == "team"
        )
        self._progress(
            f"  Permissions: {user_count} user, {team_count} team"
        )
        if stats.permissions_deduplicated:
            self._progress(
                f"Deduplicated {stats.permissions_deduplicated} "
                f"duplicate entries"
            )
        self._progress(
            f"Total unique permissions: {stats.permissions_found}"
        )

        self._save_checkpoint(
            entries,
            stats,
            completed_user_ids=list(completed_user_ids),
            completed_team_ids=list(completed_team_ids),
        )

        return entries, stats

    # ── Phase 2: Scan team memberships ────────────────────────────────

    def scan_team_memberships(self) -> list[TeamMembership]:
        self._progress("Phase 2: Scanning team memberships...")
        memberships: list[TeamMembership] = []

        teams = self._source_paginate("teams/")
        self._progress(f"  Found {len(teams)} teams")

        for team in teams:
            team_id = team["id"]
            team_name = team.get("name", f"team-{team_id}")
            team_org_id = team.get("organization") or (
                team.get("summary_fields", {})
                .get("organization", {})
                .get("id")
            )
            team_org = self._get_org_name(team_org_id)

            members = self._source_paginate(f"teams/{team_id}/users/")
            for user in members:
                memberships.append(
                    TeamMembership(
                        team_id=team_id,
                        team_name=team_name,
                        team_org=team_org,
                        user_id=user["id"],
                        username=user.get("username", f"user-{user['id']}"),
                    )
                )

            if members:
                self._progress(
                    f"  {team_name} ({team_org}): {len(members)} members"
                )
            time.sleep(self.rate_limit_delay)

        self._progress(f"Total team memberships: {len(memberships)}")
        return memberships

    # ── Phase 3: Scan system roles ────────────────────────────────────

    def scan_system_roles(self) -> list[SystemRoleEntry]:
        self._progress("Phase 3: Scanning system roles...")
        roles: list[SystemRoleEntry] = []

        for flag in ("is_superuser", "is_system_auditor"):
            users = self._source_paginate("users/", params={flag: "true"})
            for user in users:
                roles.append(
                    SystemRoleEntry(
                        user_id=user["id"],
                        username=user.get("username", f"user-{user['id']}"),
                        flag=flag,
                    )
                )
            self._progress(f"  {flag}: {len(users)} users")

        self._progress(f"Total system role entries: {len(roles)}")
        return roles

    # ── Phase 4: Detect cross-org sharing ─────────────────────────────

    @staticmethod
    def detect_cross_org_shares(
        permissions: list[PermissionEntry],
    ) -> list[CrossOrgShare]:
        tracker: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        counts: dict[tuple[str, str, str], int] = defaultdict(int)

        for entry in permissions:
            if not entry.is_cross_org:
                continue
            key = (entry.resource_type, entry.resource_name, entry.resource_org)
            tracker[key].add(entry.principal_org)
            counts[key] += 1

        shares = []
        for (rtype, rname, rorg), orgs in sorted(tracker.items()):
            shares.append(
                CrossOrgShare(
                    resource_type=rtype,
                    resource_name=rname,
                    resource_org=rorg,
                    shared_with_orgs=sorted(orgs),
                    permission_count=counts[(rtype, rname, rorg)],
                )
            )
        return shares

    # ── Phase 5: Validate prerequisites ───────────────────────────────

    def _validate_prerequisites(self) -> None:
        self._progress("Validating target prerequisites...")
        orgs = self._target_paginate("organizations/")
        if not orgs:
            raise RuntimeError(
                "No organizations found on target — "
                "run the main migration pipeline first"
            )
        self._progress(f"  Target has {len(orgs)} organizations")

        teams = self._target_paginate("teams/")
        if not teams:
            logger.warning(
                "No teams found on target — "
                "team permission migration may fail"
            )
            self._progress("  Warning: no teams found on target")
        else:
            self._progress(f"  Target has {len(teams)} teams")

    # ── Phase 6: Migrate team memberships ─────────────────────────────

    def _migrate_team_memberships(
        self,
        memberships: list[TeamMembership],
        stats: MigrationStats,
        dry_run: bool = False,
    ) -> None:
        label = "Dry-run" if dry_run else "Migrating"
        self._progress(f"Phase 6: {label} team memberships...")

        for idx, membership in enumerate(memberships):
            target_team_id = self._get_target_id(
                "teams", membership.team_id
            )
            if not target_team_id:
                target_team_id = self._discover_target_id_by_name(
                    "teams", membership.team_name
                )
            if not target_team_id:
                membership.status = "failed"
                membership.error = "Team not found on target"
                stats.team_memberships_failed += 1
                continue

            target_user_id = self._get_target_id(
                "users", membership.user_id
            )
            if not target_user_id:
                target_user_id = self._discover_target_id_by_name(
                    "users", membership.username
                )
            if not target_user_id:
                membership.status = "failed"
                membership.error = "User not found on target"
                stats.team_memberships_failed += 1
                continue

            if dry_run:
                membership.status = "dry_run"
                stats.team_memberships_migrated += 1
                continue

            endpoint = f"teams/{target_team_id}/users/"
            resp = self._target_post(endpoint, {"id": target_user_id})
            if resp is not None and resp.status_code in (401, 403):
                raise AuthenticationError(
                    endpoint,
                    resp.status_code,
                    entries_succeeded=stats.team_memberships_migrated,
                    entries_remaining=len(memberships) - idx - 1,
                )
            if resp is not None and resp.status_code in (200, 201, 204):
                membership.status = "migrated"
                stats.team_memberships_migrated += 1
            elif resp is not None and resp.status_code == 400:
                body = self._safe_json(resp) or {}
                if "already" in str(body).lower():
                    membership.status = "migrated"
                    stats.team_memberships_migrated += 1
                else:
                    membership.status = "failed"
                    membership.error = f"HTTP 400: {str(body)[:200]}"
                    stats.team_memberships_failed += 1
            else:
                code = resp.status_code if resp is not None else "no response"
                body_preview = (resp.text or "")[:500] if resp is not None else ""
                membership.status = "failed"
                membership.error = f"HTTP {code}"
                if body_preview:
                    membership.error += f" | {body_preview}"
                stats.team_memberships_failed += 1

            time.sleep(self.rate_limit_delay)

        self._progress(
            f"  Team memberships — migrated: {stats.team_memberships_migrated}, "
            f"failed: {stats.team_memberships_failed}"
        )

    # ── Phase 7: Migrate permissions ──────────────────────────────────

    def _migrate_permissions(
        self,
        permissions: list[PermissionEntry],
        stats: MigrationStats,
        dry_run: bool = False,
    ) -> None:
        label = "Dry-run" if dry_run else "Migrating"
        self._progress(f"Phase 7: {label} resource permissions...")
        target_role_cache: dict[str, dict[str, int]] = {}

        for idx, entry in enumerate(permissions):
            target_resource_id = self._get_target_id(
                entry.resource_type, entry.resource_id
            )
            if not target_resource_id:
                target_resource_id = self._discover_target_id_by_name(
                    entry.resource_type, entry.resource_name
                )
            if not target_resource_id:
                entry.status = "failed"
                entry.error = (
                    f"{entry.resource_type} not found on target"
                )
                stats.permissions_failed += 1
                continue

            if entry.principal_type == "user":
                principal_endpoint = "users"
                target_principal_id = self._get_target_id(
                    "users", entry.principal_id
                )
                if not target_principal_id:
                    target_principal_id = self._discover_target_id_by_name(
                        "users", entry.principal_name
                    )
            else:
                principal_endpoint = "teams"
                target_principal_id = self._get_target_id(
                    "teams", entry.principal_id
                )
                if not target_principal_id:
                    target_principal_id = self._discover_target_id_by_name(
                        "teams", entry.principal_name
                    )

            if not target_principal_id:
                entry.status = "failed"
                entry.error = (
                    f"{entry.principal_type} '{entry.principal_name}' "
                    f"not found on target"
                )
                stats.permissions_failed += 1
                continue

            cache_key = f"{entry.resource_type}/{target_resource_id}"
            if cache_key not in target_role_cache:
                roles_data = self._target_paginate(
                    f"{entry.resource_type}/{target_resource_id}/object_roles/"
                )
                target_role_cache[cache_key] = {
                    r["name"]: r["id"] for r in roles_data
                }

            mapped_role = self._map_role_name(entry.role_name)
            target_role_id = target_role_cache.get(cache_key, {}).get(
                mapped_role
            )
            if not target_role_id and mapped_role != entry.role_name:
                target_role_id = target_role_cache.get(cache_key, {}).get(
                    entry.role_name
                )

            if not target_role_id:
                entry.status = "failed"
                entry.error = (
                    f"Role '{entry.role_name}' not found on target resource"
                )
                stats.permissions_failed += 1
                continue

            if dry_run:
                entry.status = "dry_run"
                stats.permissions_migrated += 1
                continue

            endpoint = f"roles/{target_role_id}/{principal_endpoint}/"
            resp = self._target_post(
                endpoint,
                {"id": target_principal_id},
            )
            if resp is not None and resp.status_code in (401, 403):
                raise AuthenticationError(
                    endpoint,
                    resp.status_code,
                    entries_succeeded=stats.permissions_migrated,
                    entries_remaining=len(permissions) - idx - 1,
                )
            if resp is not None and resp.status_code in (200, 201, 204):
                entry.status = "migrated"
                stats.permissions_migrated += 1
            elif resp is not None and resp.status_code == 400:
                body = self._safe_json(resp) or {}
                if "already" in str(body).lower():
                    entry.status = "migrated"
                    stats.permissions_migrated += 1
                else:
                    entry.status = "failed"
                    entry.error = f"HTTP 400: {str(body)[:200]}"
                    stats.permissions_failed += 1
            else:
                code = resp.status_code if resp is not None else "no response"
                body_preview = (resp.text or "")[:500] if resp is not None else ""
                entry.status = "failed"
                entry.error = f"HTTP {code}"
                if body_preview:
                    entry.error += f" | {body_preview}"
                stats.permissions_failed += 1

            time.sleep(self.rate_limit_delay)

        self._progress(
            f"  Permissions — migrated: {stats.permissions_migrated}, "
            f"failed: {stats.permissions_failed}, "
            f"skipped: {stats.permissions_skipped}"
        )

    # ── Aggregation ───────────────────────────────────────────────────

    @staticmethod
    def build_org_summaries(
        permissions: list[PermissionEntry],
        memberships: list[TeamMembership],
        cross_org_shares: list[CrossOrgShare],
    ) -> dict[str, OrgSummary]:
        summaries: dict[str, OrgSummary] = {}
        resources_by_org: dict[str, set[int]] = defaultdict(set)

        for p in permissions:
            org = p.resource_org
            if org not in summaries:
                summaries[org] = OrgSummary(org_name=org)
            s = summaries[org]
            resources_by_org[org].add(p.resource_id)
            s.permissions_total += 1
            s.permissions_by_type[p.resource_type] = (
                s.permissions_by_type.get(p.resource_type, 0) + 1
            )
            s.permissions_by_role[p.role_name] = (
                s.permissions_by_role.get(p.role_name, 0) + 1
            )
            if p.status == "migrated":
                s.permissions_migrated += 1
            elif p.status == "failed":
                s.permissions_failed += 1
            elif p.status in ("skipped", "audit", "dry_run"):
                s.permissions_skipped += 1

        for org, res_ids in resources_by_org.items():
            summaries[org].resources_scanned = len(res_ids)

        for m in memberships:
            org = m.team_org
            if org not in summaries:
                summaries[org] = OrgSummary(org_name=org)
            s = summaries[org]
            s.team_memberships_total += 1
            if m.status == "migrated":
                s.team_memberships_migrated += 1
            elif m.status == "failed":
                s.team_memberships_failed += 1

        for c in cross_org_shares:
            if c.resource_org in summaries:
                summaries[c.resource_org].cross_org_shares += 1

        return summaries

    # ── Orchestration ─────────────────────────────────────────────────

    def audit(self) -> IAMAuditResult:
        """Read-only scan of source AAP — no target access required."""
        self._progress("Starting IAM audit (read-only)...")
        self._progress(f"Source: {self.source_url}")
        self._progress(f"Scan strategy: {self.scan_strategy}")
        if self._resume:
            self._progress("Resume mode: ON")
        if self._checkpoint_path:
            self._progress(f"Checkpoint: {self._checkpoint_path}")

        if self.scan_strategy == "resource":
            self._progress(
                "WARNING: Resource strategy can take many hours on large "
                "environments. Ensure your bearer token will not expire "
                "during the scan. Use --resume to recover from interruptions."
            )

        if self.scan_strategy == "principal":
            permissions, stats = self.scan_permissions_principal()
        else:
            permissions, stats = self.scan_permissions()
        memberships = self.scan_team_memberships()
        system_roles = self.scan_system_roles()
        cross_org_shares = self.detect_cross_org_shares(permissions)

        for p in permissions:
            p.status = "audit"
        for m in memberships:
            m.status = "audit"

        stats.team_memberships_found = len(memberships)
        stats.system_roles_found = len(system_roles)
        stats.cross_org_shares = len(cross_org_shares)

        org_summaries = self.build_org_summaries(
            permissions, memberships, cross_org_shares
        )

        self._progress("Audit complete.")
        return IAMAuditResult(
            mode="audit",
            source_url=self.source_url,
            permissions=permissions,
            team_memberships=memberships,
            system_roles=system_roles,
            cross_org_shares=cross_org_shares,
            org_summaries=org_summaries,
            stats=stats,
        )

    def migrate(
        self,
        dry_run: bool = False,
        skip_user_roles: bool = False,
        users_only: bool = False,
    ) -> IAMAuditResult:
        """Scan source, then apply permissions to target.

        Args:
            dry_run: Show what would be assigned without making changes.
            skip_user_roles: Migrate team-based permissions only. User
                permissions and team memberships (user→team) are marked
                as 'pending' for a later --users-only pass.
            users_only: Migrate only user-based permissions and team
                memberships. Skips team-based resource permissions
                (already done in a prior --skip-user-roles pass).
        """
        if not self.target_url or not self.target_token:
            raise RuntimeError(
                "Target URL and token required for migration"
            )
        if skip_user_roles and users_only:
            raise ValueError(
                "--skip-user-roles and --users-only are mutually exclusive"
            )

        mode = "dry_run" if dry_run else "migrate"
        label = "dry-run" if dry_run else "migration"
        if skip_user_roles:
            label = f"{label} (teams only)"
        elif users_only:
            label = f"{label} (users only)"
        self._progress(f"Starting IAM {label}...")
        self._progress(f"Source: {self.source_url}")
        self._progress(f"Target: {self.target_url}")
        self._progress(f"Scan strategy: {self.scan_strategy}")

        if self.scan_strategy == "principal":
            permissions, stats = self.scan_permissions_principal()
        else:
            permissions, stats = self.scan_permissions()
        memberships = self.scan_team_memberships()
        system_roles = self.scan_system_roles()
        cross_org_shares = self.detect_cross_org_shares(permissions)

        stats.team_memberships_found = len(memberships)
        stats.system_roles_found = len(system_roles)
        stats.cross_org_shares = len(cross_org_shares)

        user_perms = [p for p in permissions if p.principal_type == "user"]
        team_perms = [p for p in permissions if p.principal_type == "team"]
        stats.user_permissions_total = len(user_perms)
        stats.team_permissions_total = len(team_perms)

        self._validate_prerequisites()

        if skip_user_roles:
            for p in user_perms:
                p.status = "pending"
                stats.permissions_skipped += 1
            stats.user_permissions_pending = len(user_perms)
            self._progress(
                f"  Skipping {len(user_perms)} user-based permissions "
                f"(use --users-only later)"
            )
            for m in memberships:
                m.status = "pending"
            stats.team_memberships_skipped = len(memberships)
            self._progress(
                f"  Skipping {len(memberships)} team memberships "
                f"(use --users-only later)"
            )
            self._migrate_permissions(team_perms, stats, dry_run=dry_run)

        elif users_only:
            for p in team_perms:
                p.status = "skipped"
                stats.permissions_skipped += 1
            self._progress(
                f"  Skipping {len(team_perms)} team-based permissions "
                f"(already migrated)"
            )
            self._migrate_team_memberships(
                memberships, stats, dry_run=dry_run
            )
            self._migrate_permissions(user_perms, stats, dry_run=dry_run)

        else:
            self._migrate_team_memberships(
                memberships, stats, dry_run=dry_run
            )
            self._migrate_permissions(permissions, stats, dry_run=dry_run)

        org_summaries = self.build_org_summaries(
            permissions, memberships, cross_org_shares
        )

        self._progress(f"IAM {label} complete.")
        return IAMAuditResult(
            mode=mode,
            source_url=self.source_url,
            permissions=permissions,
            team_memberships=memberships,
            system_roles=system_roles,
            cross_org_shares=cross_org_shares,
            org_summaries=org_summaries,
            stats=stats,
        )
