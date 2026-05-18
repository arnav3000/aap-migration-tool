"""
Dynamic Sizing: Gather live metrics from an AAP instance to auto-calculate
recommended AAP 2.6 sizing based on actual workload history.

Connects to the source AAP API, inspects jobs history, instances, instance groups,
hosts, and configuration to derive sizing inputs automatically.

Designed for large environments (100k+ jobs): uses stratified per-day sampling
to derive accurate daily counts and duration/hour-of-day statistics without
fetching every job record.  Total API calls scale with *days* not *jobs*.
"""

import logging
import math
import random
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from aap_migration.sizing.calculator import AAP26SizingCalculator

logger = logging.getLogger(__name__)

HEADROOM_MULTIPLIER = 1.25  # 25% buffer above observed peak

_RESERVOIR_SIZE = 2000
_SAMPLE_PAGES_PER_DAY = 3  # pages of 200 to sample per day for duration/hour stats
_MAX_CONCURRENT_REQUESTS = 6


class _OnlineStats:
    """Welford's online algorithm for streaming mean and variance."""

    __slots__ = ("n", "mean", "_m2")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self._m2 = 0.0

    def update(self, value: float) -> None:
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        delta2 = value - self.mean
        self._m2 += delta * delta2

    @property
    def variance(self) -> float:
        return self._m2 / self.n if self.n > 1 else 0.0

    @property
    def stddev(self) -> float:
        return math.sqrt(self.variance)


class _ReservoirSampler:
    """Reservoir sampling (Algorithm R) for approximate percentiles on streams."""

    __slots__ = ("_reservoir", "_k", "_n")

    def __init__(self, k: int = _RESERVOIR_SIZE) -> None:
        self._reservoir: list[float] = []
        self._k = k
        self._n = 0

    def add(self, value: float) -> None:
        self._n += 1
        if self._n <= self._k:
            self._reservoir.append(value)
        else:
            j = random.randint(0, self._n - 1)  # nosec B311 — statistical sampling, not crypto
            if j < self._k:
                self._reservoir[j] = value

    def percentile(self, p: float) -> float:
        """Return approximate p-th percentile (p in 0..1)."""
        if not self._reservoir:
            return 0.0
        sorted_vals = sorted(self._reservoir)
        idx = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
        return sorted_vals[idx]

    @property
    def count(self) -> int:
        return self._n


class DynamicSizingCollector:
    """Collects live metrics from an AAP instance for dynamic sizing calculations."""

    def __init__(
        self,
        base_url: str,
        token: str,
        api_prefix: str | None = None,
        verify_ssl: bool = True,
        timeout: int = 60,
        auth_scheme: str = "Bearer",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_prefix = api_prefix or ""
        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.auth_scheme = auth_scheme

    @property
    def api_url(self) -> str:
        return f"{self.base_url}{self.api_prefix}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"{self.auth_scheme} {self.token}",
            "Content-Type": "application/json",
        }

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        logger.debug("GET %s params=%s", url, params)
        resp = httpx.get(
            url,
            headers=self._headers(),
            params=params,
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    def _get_paginated_all(
        self, endpoint: str, params: dict[str, Any] | None = None, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        query = params.copy() if params else {}
        query["page_size"] = 200
        page = 1

        while page <= max_pages:
            query["page"] = page
            data = self._get(endpoint, params=query)
            items = data.get("results", [])
            results.extend(items)
            if not data.get("next"):
                break
            page += 1

        return results

    # ------------------------------------------------------------------
    # Individual metric collectors (non-critical silently degrade)
    # ------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        try:
            return self._get("config/")
        except Exception:
            logger.warning("Failed to fetch AAP config", exc_info=True)
            return {}

    def get_instances(self) -> list[dict[str, Any]]:
        try:
            return self._get_paginated_all("instances/")
        except Exception:
            logger.warning("Failed to fetch instances", exc_info=True)
            return []

    def get_instance_groups(self) -> list[dict[str, Any]]:
        try:
            return self._get_paginated_all("instance_groups/")
        except Exception:
            logger.warning("Failed to fetch instance groups", exc_info=True)
            return []

    def get_hosts_count(self) -> int:
        try:
            data = self._get("hosts/", params={"page_size": 1})
            return int(data.get("count", 0))
        except Exception:
            logger.warning("Failed to fetch hosts count", exc_info=True)
            return 0

    def get_inventories_summary(self) -> dict[str, Any]:
        try:
            data = self._get("inventories/", params={"page_size": 1})
            return {"inventory_count": data.get("count", 0)}
        except Exception:
            logger.warning("Failed to fetch inventories", exc_info=True)
            return {"inventory_count": 0}

    def get_job_templates_summary(self) -> dict[str, Any]:
        try:
            templates = self._get_paginated_all("job_templates/", max_pages=10)
            forks_values = [t.get("forks", 0) for t in templates if t.get("forks", 0) > 0]
            verbosity_values = [t.get("verbosity", 1) for t in templates]
            return {
                "template_count": len(templates),
                "forks_values": forks_values,
                "avg_forks": statistics.mean(forks_values) if forks_values else 5,
                "median_forks": statistics.median(forks_values) if forks_values else 5,
                "max_forks": max(forks_values) if forks_values else 5,
                "avg_verbosity": (
                    round(statistics.mean(verbosity_values)) if verbosity_values else 1
                ),
            }
        except Exception:
            logger.warning("Failed to fetch job templates", exc_info=True)
            return {
                "template_count": 0,
                "forks_values": [],
                "avg_forks": 5,
                "median_forks": 5,
                "max_forks": 5,
                "avg_verbosity": 1,
            }

    def get_settings_jobs(self) -> dict[str, Any]:
        try:
            data = self._get("settings/jobs/")
            return {
                "max_forks": data.get("DEFAULT_JOB_FORKS", 5),
                "job_retention_days": data.get("DAYS_TO_KEEP_LAST_JOB", 30),
            }
        except Exception:
            logger.warning("Failed to fetch job settings", exc_info=True)
            return {"max_forks": 5, "job_retention_days": 30}

    # ------------------------------------------------------------------
    # Stratified job history analysis
    # ------------------------------------------------------------------

    def _fetch_day_stats(self, day_start: str, day_end: str) -> dict[str, Any]:
        """Fetch job count + sample for a single day window.

        Makes 1 request to get the total count, then up to
        _SAMPLE_PAGES_PER_DAY - 1 more pages for duration / hour samples.
        """
        params: dict[str, Any] = {
            "status": "successful",
            "finished__gt": day_start,
            "finished__lt": day_end,
            "order_by": "-finished",
            "page_size": 200,
        }

        data = self._get("jobs/", params=params)
        total_count = data.get("count", 0)
        sampled_jobs: list[dict[str, Any]] = data.get("results", [])

        pages_fetched = 1
        while pages_fetched < _SAMPLE_PAGES_PER_DAY and data.get("next"):
            pages_fetched += 1
            params["page"] = pages_fetched
            data = self._get("jobs/", params=params)
            sampled_jobs.extend(data.get("results", []))

        return {"total_count": total_count, "sampled_jobs": sampled_jobs}

    def get_job_history_stratified(self, days: int = 30) -> dict[str, Any]:
        """Analyze job history using per-day stratified sampling.

        Instead of paginating through every job (100k+ = 500+ API calls),
        issues one query per day to get the exact count from the API's
        ``count`` field, plus a small sample of actual job records for
        duration and hour-of-day statistics.

        For 30 days of history this is ~30-90 API calls total regardless
        of how many jobs exist, and the daily counts are exact.
        """
        now = datetime.now(UTC)
        day_windows: list[tuple[str, str, str]] = []
        for offset in range(days):
            day = now - timedelta(days=offset)
            day_key = day.strftime("%Y-%m-%d")
            start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            day_windows.append(
                (
                    day_key,
                    start.strftime("%Y-%m-%dT%H:%M:%S"),
                    end.strftime("%Y-%m-%dT%H:%M:%S"),
                )
            )

        daily_counts: dict[str, int] = {}
        hourly_distribution: dict[int, int] = dict.fromkeys(range(24), 0)
        duration_stats = _OnlineStats()
        duration_sampler = _ReservoirSampler(_RESERVOIR_SIZE)
        total_jobs = 0
        days_with_data = 0

        def _process_day(window: tuple[str, str, str]) -> tuple[str, dict[str, Any]] | None:
            day_key, day_start, day_end = window
            try:
                return day_key, self._fetch_day_stats(day_start, day_end)
            except Exception:
                logger.warning("Failed to fetch jobs for %s", day_key, exc_info=True)
                return None

        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_REQUESTS) as pool:
            futures = {pool.submit(_process_day, w): w for w in day_windows}
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue

                day_key, day_data = result
                count = day_data["total_count"]
                if count == 0:
                    continue

                daily_counts[day_key] = count
                total_jobs += count
                days_with_data += 1

                for job in day_data["sampled_jobs"]:
                    started = job.get("started")
                    finished = job.get("finished")

                    if started:
                        try:
                            start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                            hourly_distribution[start_dt.hour] += 1
                        except (ValueError, AttributeError):
                            pass

                    if started and finished:
                        try:
                            start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                            end_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                            duration_hours = (end_dt - start_dt).total_seconds() / 3600
                            if 0 < duration_hours < 24:
                                duration_stats.update(duration_hours)
                                duration_sampler.add(duration_hours)
                        except (ValueError, AttributeError):
                            pass

        logger.info(
            "Job history stratified: %d total jobs across %d days (%d days with data, "
            "%d duration samples)",
            total_jobs,
            days,
            days_with_data,
            duration_stats.n,
        )

        day_values = list(daily_counts.values()) if daily_counts else [0]
        playbooks_per_day_peak = max(day_values) if day_values else 0
        playbooks_per_day_avg = statistics.mean(day_values) if day_values else 0

        avg_duration = duration_stats.mean if duration_stats.n > 0 else 0.25
        p95_duration = (
            duration_sampler.percentile(0.95) if duration_sampler.count > 10 else avg_duration
        )

        peak_pattern = self._detect_peak_pattern(hourly_distribution)
        analysis_days = len(daily_counts) if daily_counts else 1

        return {
            "playbooks_per_day_peak": playbooks_per_day_peak,
            "playbooks_per_day_avg": round(playbooks_per_day_avg, 1),
            "job_duration_hours": round(avg_duration, 4),
            "job_duration_p95_hours": round(p95_duration, 4),
            "peak_pattern": peak_pattern,
            "jobs_analyzed": total_jobs,
            "jobs_sampled_for_duration": duration_stats.n,
            "analysis_days": analysis_days,
            "daily_counts": daily_counts,
            "hourly_distribution": hourly_distribution,
        }

    # ------------------------------------------------------------------
    # Pattern detection helpers
    # ------------------------------------------------------------------

    def _detect_peak_pattern(self, hourly_distribution: dict[int, int]) -> str:
        total_jobs = sum(hourly_distribution.values())
        if total_jobs == 0:
            return "business_hours"

        business_hours_jobs = sum(hourly_distribution.get(h, 0) for h in range(8, 18))
        business_ratio = business_hours_jobs / total_jobs

        sorted_hours = sorted(hourly_distribution.values(), reverse=True)
        top_4_hours = sum(sorted_hours[:4])
        batch_ratio = top_4_hours / total_jobs

        if batch_ratio > 0.7:
            return "batch_window"
        elif business_ratio > 0.75:
            return "business_hours"
        elif business_ratio < 0.45:
            return "distributed_24x7"
        else:
            return "mixed"

    def _detect_allowed_hours(self, hourly_distribution: dict[int, int]) -> int:
        total_jobs = sum(hourly_distribution.values())
        if total_jobs == 0:
            return 24

        threshold = total_jobs * 0.02
        active_hours = sum(1 for count in hourly_distribution.values() if count > threshold)
        return max(4, min(24, active_hours))

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    def collect_all_metrics(self, history_days: int = 30) -> dict[str, Any]:
        """Collect all metrics from the live AAP instance.

        Returns a complete metrics dict ready for the sizing calculator,
        along with raw observed data for transparency.
        """
        logger.info("Connecting to AAP at %s", self.api_url)
        config = self._get("config/")
        logger.info("Connected — AAP version %s", config.get("version", "unknown"))

        # Fetch non-job metadata in parallel
        metadata: dict[str, Any] = {}
        metadata_tasks = {
            "instances": lambda: self.get_instances(),
            "instance_groups": lambda: self.get_instance_groups(),
            "managed_hosts": lambda: self.get_hosts_count(),
            "inventories": lambda: self.get_inventories_summary(),
            "job_templates": lambda: self.get_job_templates_summary(),
            "settings": lambda: self.get_settings_jobs(),
        }
        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_REQUESTS) as pool:
            futures = {pool.submit(fn): key for key, fn in metadata_tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    metadata[key] = future.result()
                except Exception:
                    logger.warning("Failed to collect %s", key, exc_info=True)
                    metadata[key] = None

        instances = metadata.get("instances") or []
        instance_groups = metadata.get("instance_groups") or []
        managed_hosts = metadata.get("managed_hosts") or 0
        inventories = metadata.get("inventories") or {"inventory_count": 0}
        job_templates = metadata.get("job_templates") or {
            "template_count": 0,
            "avg_forks": 5,
            "median_forks": 5,
            "max_forks": 5,
            "avg_verbosity": 1,
        }
        settings = metadata.get("settings") or {"max_forks": 5, "job_retention_days": 30}

        # Stratified job history (the heavy part — parallel per-day queries)
        job_patterns = self.get_job_history_stratified(days=history_days)

        # Categorize instances
        execution_instances = [
            i for i in instances if i.get("node_type") in ("execution", "hybrid")
        ]
        control_instances = [i for i in instances if i.get("node_type") in ("control", "hybrid")]

        def _num(val: Any, default: float = 0) -> float:
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        total_current_cpu = sum(_num(i.get("cpu")) for i in instances)
        total_current_memory = sum(_num(i.get("memory")) for i in instances)
        total_current_memory_gb = (
            total_current_memory / (1024**3) if total_current_memory > 0 else 0
        )

        allowed_hours = self._detect_allowed_hours(job_patterns.get("hourly_distribution", {}))
        peak_with_headroom = math.ceil(job_patterns["playbooks_per_day_peak"] * HEADROOM_MULTIPLIER)

        observed_forks = max(
            int(job_templates.get("avg_forks", 5)),
            settings.get("max_forks", 5),
        )
        job_retention_hours = settings.get("job_retention_days", 30) * 24

        return {
            "observed": {
                "version": config.get("version"),
                "license_type": config.get("license_info", {}).get("license_type"),
                "total_instances": len(instances),
                "execution_instances": len(execution_instances),
                "control_instances": len(control_instances),
                "instance_groups": len(instance_groups),
                "instance_group_names": [ig.get("name") for ig in instance_groups],
                "managed_hosts": managed_hosts,
                "inventories": inventories.get("inventory_count", 0),
                "job_templates": job_templates.get("template_count", 0),
                "total_current_cpu": total_current_cpu,
                "total_current_memory_gb": round(total_current_memory_gb, 1),
                "jobs_analyzed": job_patterns["jobs_analyzed"],
                "jobs_sampled_for_duration": job_patterns.get("jobs_sampled_for_duration", 0),
                "analysis_days": job_patterns["analysis_days"],
                "playbooks_per_day_peak": job_patterns["playbooks_per_day_peak"],
                "playbooks_per_day_avg": job_patterns["playbooks_per_day_avg"],
                "job_duration_hours_avg": job_patterns["job_duration_hours"],
                "job_duration_hours_p95": job_patterns.get("job_duration_p95_hours"),
                "detected_peak_pattern": job_patterns["peak_pattern"],
                "detected_allowed_hours": allowed_hours,
                "avg_forks_configured": job_templates.get("avg_forks"),
                "max_forks_configured": job_templates.get("max_forks"),
                "avg_verbosity": job_templates.get("avg_verbosity", 1),
                "hourly_distribution": job_patterns.get("hourly_distribution"),
            },
            "sizing_inputs": {
                "managed_hosts": managed_hosts,
                "playbooks_per_day_peak": peak_with_headroom,
                "job_duration_hours": job_patterns["job_duration_hours"],
                "tasks_per_job": 50,
                "forks_observed": observed_forks,
                "verbosity_level": job_templates.get("avg_verbosity", 1),
                "allowed_hours_per_day": allowed_hours,
                "peak_pattern": job_patterns["peak_pattern"],
                "job_retention_hours": job_retention_hours,
                "num_controllers": max(2, len(control_instances)),
                "num_hub_nodes": 1,
                "hub_cpu_percent": 25,
                "hub_memory_percent": 30,
                "database_vcpu": 8,
                "database_memory_gb": 64,
                "database_cpu_percent": 50,
                "database_memory_percent": 35,
            },
            "headroom_applied": HEADROOM_MULTIPLIER,
        }


def calculate_dynamic_sizing(
    base_url: str,
    token: str,
    api_prefix: str | None = None,
    verify_ssl: bool = True,
    history_days: int = 30,
    deployment_target: str = "ocp",
    auth_scheme: str = "Bearer",
) -> dict[str, Any]:
    """Run dynamic sizing: collect metrics from live AAP and produce recommendations."""
    collector = DynamicSizingCollector(
        base_url=base_url,
        token=token,
        api_prefix=api_prefix,
        verify_ssl=verify_ssl,
        auth_scheme=auth_scheme,
    )

    metrics = collector.collect_all_metrics(history_days=history_days)
    sizing_inputs = metrics["sizing_inputs"]

    calculator = AAP26SizingCalculator()
    recommendation = calculator.generate_sizing_recommendation(sizing_inputs, deployment_target)

    _enforce_minimums(recommendation, deployment_target)

    return {
        "mode": "dynamic",
        "deployment_target": deployment_target,
        "source_observed": metrics["observed"],
        "derived_inputs": sizing_inputs,
        "headroom_multiplier": metrics["headroom_applied"],
        "recommendation": recommendation,
    }


# AAP 2.6 minimum specs per Red Hat documentation
MIN_SPECS_OCP = {
    "platform_gateway": {"cpu_per_pod": 2, "memory_per_pod_gb": 4},
    "automation_controller_control_plane": {"cpu_per_pod": 2, "memory_per_pod_gb": 4},
    "automation_controller_execution_plane": {"cpu_per_pod": 2, "memory_per_pod_gb": 4},
    "automation_hub": {"cpu_per_pod": 2, "memory_per_pod_gb": 4},
    "event_driven_ansible": {"cpu_per_pod": 2, "memory_per_pod_gb": 4},
    "database": {"cpu": 4, "memory_gb": 16, "storage_gb": 60},
    "redis": {"total_cpu": 1, "total_memory_gb": 2},
}

MIN_SPECS_CONTAINERIZED = {
    "platform_gateway": {"cpu_per_pod": 4, "memory_per_pod_gb": 16},
    "automation_controller_control_plane": {"cpu_per_pod": 4, "memory_per_pod_gb": 16},
    "automation_controller_execution_plane": {"cpu_per_pod": 4, "memory_per_pod_gb": 16},
    "automation_hub": {"cpu_per_pod": 4, "memory_per_pod_gb": 16},
    "event_driven_ansible": {"cpu_per_pod": 4, "memory_per_pod_gb": 16},
    "database": {"cpu": 4, "memory_gb": 16, "storage_gb": 60},
    "redis": {"total_cpu": 1, "total_memory_gb": 2},
}


def _enforce_minimums(recommendation: dict[str, Any], deployment_target: str = "ocp") -> None:
    """Ensure no component goes below AAP 2.6 minimum specs."""
    min_specs = MIN_SPECS_OCP if deployment_target == "ocp" else MIN_SPECS_CONTAINERIZED
    components = recommendation.get("components", {})
    for comp_name, mins in min_specs.items():
        comp = components.get(comp_name)
        if not comp:
            continue
        for key, min_val in mins.items():
            if key in comp and comp[key] < min_val:
                comp[key] = min_val
        if "cpu_per_pod" in mins and "total_cpu" in comp:
            pod_key = next((k for k in comp if k.endswith("_pods") or k == "execution_pods"), None)
            if pod_key:
                comp["total_cpu"] = max(comp["total_cpu"], comp[pod_key] * comp["cpu_per_pod"])
        if "memory_per_pod_gb" in mins and "total_memory_gb" in comp:
            pod_key = next((k for k in comp if k.endswith("_pods") or k == "execution_pods"), None)
            if pod_key:
                comp["total_memory_gb"] = max(
                    comp["total_memory_gb"], comp[pod_key] * comp["memory_per_pod_gb"]
                )
