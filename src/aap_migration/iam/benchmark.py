"""IAM analyser benchmark — measures actual API performance.

Run inside the container against the real AAP 2.4 instance to get
real response times and concurrency capacity before committing to
a scan strategy.

Usage:
    aap-bridge iam benchmark --no-verify-ssl
    aap-bridge iam benchmark --no-verify-ssl --workers 20
"""

from __future__ import annotations

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _create_session(verify_ssl: bool = True) -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=60)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if not verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def _api_get(
    session: requests.Session,
    base_url: str,
    token: str,
    endpoint: str,
    verify_ssl: bool,
    timeout: int = 30,
) -> tuple[float, int, dict | None]:
    """Single API call. Returns (elapsed_seconds, status_code, data)."""
    url = f"{base_url}/{endpoint.lstrip('/')}"
    start = time.monotonic()
    try:
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": 200},
            verify=verify_ssl,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        data = resp.json() if resp.status_code == 200 and resp.text else None
        return elapsed, resp.status_code, data
    except Exception as exc:
        elapsed = time.monotonic() - start
        return elapsed, 0, None


def run_benchmark(
    source_url: str,
    source_token: str,
    verify_ssl: bool = True,
    sample_size: int = 50,
    worker_counts: list[int] | None = None,
) -> None:
    if worker_counts is None:
        worker_counts = [1, 10, 20]

    source_url = source_url.rstrip("/")
    session = _create_session(verify_ssl)

    print("=" * 70)
    print("  IAM ANALYSER BENCHMARK")
    print("=" * 70)
    print(f"  Source: {source_url}")
    print()

    # ── Step 1: Collect sample role IDs ────────────────────────────────
    print("Step 1: Collecting sample role IDs...")

    # Grab a few resources of different types and extract role IDs
    role_ids: list[int] = []
    resource_types_tested = []

    for rtype in ["credentials", "projects", "inventories", "job_templates", "organizations"]:
        elapsed, status, data = _api_get(
            session, source_url, source_token,
            f"{rtype}/?page_size=5", verify_ssl,
        )
        if status != 200 or not data:
            print(f"  {rtype}: HTTP {status} ({elapsed:.3f}s) — skipping")
            continue

        resources = data.get("results", [])
        print(f"  {rtype}: fetched {len(resources)} sample resources ({elapsed:.3f}s)")

        for resource in resources[:3]:
            res_id = resource["id"]
            e2, s2, d2 = _api_get(
                session, source_url, source_token,
                f"{rtype}/{res_id}/object_roles/", verify_ssl,
            )
            if s2 == 200 and d2:
                for role in d2.get("results", []):
                    role_ids.append(role["id"])
                resource_types_tested.append(rtype)

    if not role_ids:
        print("\n  ERROR: Could not collect any role IDs. Check credentials.")
        return

    # Deduplicate and sample
    role_ids = list(set(role_ids))
    if len(role_ids) > sample_size:
        role_ids = random.sample(role_ids, sample_size)

    print(f"\n  Collected {len(role_ids)} unique role IDs for benchmarking")
    print(f"  Resource types sampled: {', '.join(set(resource_types_tested))}")
    print()

    # ── Step 2: Sequential baseline ───────────────────────────────────
    print("Step 2: Sequential baseline (1 worker)...")

    endpoints = []
    for rid in role_ids:
        endpoints.append(f"roles/{rid}/users/")
        endpoints.append(f"roles/{rid}/teams/")

    total_calls = len(endpoints)
    timings: list[float] = []
    empty_count = 0
    non_empty_count = 0
    error_count = 0

    for i, ep in enumerate(endpoints):
        elapsed, status, data = _api_get(
            session, source_url, source_token, ep, verify_ssl,
        )
        timings.append(elapsed)
        if status == 200 and data:
            count = data.get("count", 0)
            if count == 0:
                empty_count += 1
            else:
                non_empty_count += 1
        else:
            error_count += 1

        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{total_calls} calls...", end="\r")

    avg_time = sum(timings) / len(timings) if timings else 0
    p50 = sorted(timings)[len(timings) // 2] if timings else 0
    p95 = sorted(timings)[int(len(timings) * 0.95)] if timings else 0
    p99 = sorted(timings)[int(len(timings) * 0.99)] if timings else 0

    empty_pct = (empty_count / (empty_count + non_empty_count) * 100) if (empty_count + non_empty_count) else 0

    print(f"\n  Calls made:         {total_calls}")
    print(f"  Empty responses:    {empty_count} ({empty_pct:.1f}%)")
    print(f"  Non-empty:          {non_empty_count}")
    print(f"  Errors:             {error_count}")
    print(f"  Avg response:       {avg_time * 1000:.0f} ms")
    print(f"  P50:                {p50 * 1000:.0f} ms")
    print(f"  P95:                {p95 * 1000:.0f} ms")
    print(f"  P99:                {p99 * 1000:.0f} ms")
    print(f"  Sequential RPS:     {1 / avg_time:.1f} req/s")
    print()

    # ── Step 3: Concurrent tests ──────────────────────────────────────
    concurrency_results: dict[int, dict[str, float]] = {}
    seq_total_time = sum(timings)

    for workers in worker_counts:
        if workers <= 1:
            continue

        print(f"Step 3: Concurrent test ({workers} workers)...")

        concurrent_timings: list[float] = []
        concurrent_errors = 0

        def do_call(ep: str) -> tuple[float, int]:
            e, s, _ = _api_get(session, source_url, source_token, ep, verify_ssl)
            return e, s

        wall_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(do_call, ep): ep for ep in endpoints}
            for future in as_completed(futures):
                elapsed, status = future.result()
                concurrent_timings.append(elapsed)
                if status != 200:
                    concurrent_errors += 1
        wall_elapsed = time.monotonic() - wall_start

        c_avg = sum(concurrent_timings) / len(concurrent_timings) if concurrent_timings else 0
        c_p95 = sorted(concurrent_timings)[int(len(concurrent_timings) * 0.95)] if concurrent_timings else 0
        actual_rps = total_calls / wall_elapsed if wall_elapsed else 0
        speedup = seq_total_time / wall_elapsed if wall_elapsed > 0 else 1.0

        concurrency_results[workers] = {
            "wall_elapsed": wall_elapsed,
            "speedup": speedup,
            "avg": c_avg,
            "p95": c_p95,
            "rps": actual_rps,
            "errors": concurrent_errors,
        }

        print(f"  Wall-clock time:    {wall_elapsed:.1f}s (vs {seq_total_time:.1f}s sequential)")
        print(f"  Speedup:            {speedup:.1f}x")
        print(f"  Avg response:       {c_avg * 1000:.0f} ms")
        print(f"  P95:                {c_p95 * 1000:.0f} ms")
        print(f"  Actual RPS:         {actual_rps:.1f} req/s")
        print(f"  Errors:             {concurrent_errors}")
        print()

    # ── Step 4: Extrapolation ─────────────────────────────────────────
    total_role_ids_est = 474171  # from export analysis
    total_membership_calls = total_role_ids_est * 2

    print("=" * 70)
    print("  EXTRAPOLATION TO FULL ENVIRONMENT")
    print("=" * 70)
    print(f"  Estimated role IDs:     {total_role_ids_est:,}")
    print(f"  Membership API calls:   {total_membership_calls:,}")
    print(f"  Empty response ratio:   {empty_pct:.1f}%")
    print()
    print(f"  {'Mode':<25} {'Time':>12} {'RPS':>10}")
    print(f"  {'-' * 25} {'-' * 12} {'-' * 10}")

    seq_time = total_membership_calls * avg_time
    print(f"  {'Sequential (1 worker)':<25} {seq_time / 3600:>10.1f} h {1 / avg_time:>9.1f}")

    for workers, result in sorted(concurrency_results.items()):
        est_time = seq_time / result["speedup"]
        est_rps = total_membership_calls / est_time if est_time > 0 else 0
        print(f"  {f'{workers} workers':<25} {est_time / 3600:>10.1f} h {est_rps:>9.1f}")

    print()
    print("  NOTE: These are estimates based on the sample.")
    print("  Actual times may vary with server load and network.")
    print("=" * 70)

    session.close()
