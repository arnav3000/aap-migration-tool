from __future__ import annotations

from aap_migration.sizing.dynamic import (
    DynamicSizingCollector,
    _enforce_minimums,
    _OnlineStats,
    _ReservoirSampler,
    calculate_dynamic_sizing,
)


def test_online_stats_and_reservoir_sampler(monkeypatch):
    stats = _OnlineStats()
    stats.update(1.0)
    stats.update(3.0)
    stats.update(5.0)
    assert round(stats.mean, 2) == 3.0
    assert round(stats.variance, 2) == 2.67
    assert round(stats.stddev, 3) > 1.6

    sampler = _ReservoirSampler(k=3)
    monkeypatch.setattr("aap_migration.sizing.dynamic.random.randint", lambda a, b: 1)
    for value in [10.0, 20.0, 30.0, 40.0]:
        sampler.add(value)
    assert sampler.count == 4
    assert sampler.percentile(0.5) in {20.0, 30.0, 40.0}
    assert _ReservoirSampler(k=2).percentile(0.5) == 0.0


def test_dynamic_collector_getters_and_fetch_day_stats(monkeypatch):
    collector = DynamicSizingCollector(
        base_url="https://aap.example.com/",
        token="secret",
        api_prefix="/api/v2",
        verify_ssl=False,
        auth_scheme="Token",
    )
    assert collector.api_url == "https://aap.example.com/api/v2"
    assert collector._headers()["Authorization"] == "Token secret"

    responses = {
        ("config/", None): {"version": "2.4"},
        ("instances/", None): {"results": [{"id": 1}], "next": None},
        ("instance_groups/", None): {"results": [{"id": 2}], "next": None},
        ("hosts/", (("page_size", 1),)): {"count": 44},
        ("inventories/", (("page_size", 1),)): {"count": 5},
        ("settings/jobs/", None): {"DEFAULT_JOB_FORKS": 7, "DAYS_TO_KEEP_LAST_JOB": 14},
        (
            "jobs/",
            (
                ("finished__gt", "2026-05-17T00:00:00"),
                ("finished__lt", "2026-05-18T00:00:00"),
                ("order_by", "-finished"),
                ("page_size", 200),
                ("status", "successful"),
            ),
        ): {
            "count": 2,
            "results": [{"started": "2026-05-17T08:00:00Z", "finished": "2026-05-17T10:00:00Z"}],
            "next": "page2",
        },
        (
            "jobs/",
            (
                ("finished__gt", "2026-05-17T00:00:00"),
                ("finished__lt", "2026-05-18T00:00:00"),
                ("order_by", "-finished"),
                ("page", 2),
                ("page_size", 200),
                ("status", "successful"),
            ),
        ): {
            "count": 2,
            "results": [{"started": "2026-05-17T12:00:00Z", "finished": "2026-05-17T13:00:00Z"}],
            "next": None,
        },
    }

    def fake_get(endpoint, params=None):
        key = (endpoint, tuple(sorted((params or {}).items())) or None)
        if key == ("job_templates/", None):
            raise RuntimeError("template failure")
        return responses[key]

    def fake_paginated(endpoint, params=None, max_pages=50):
        if endpoint == "job_templates/":
            raise RuntimeError("template failure")
        return fake_get(endpoint, params).get("results", [])

    monkeypatch.setattr(collector, "_get", fake_get)
    monkeypatch.setattr(collector, "_get_paginated_all", fake_paginated)

    assert collector.get_config() == {"version": "2.4"}
    assert collector.get_instances() == [{"id": 1}]
    assert collector.get_instance_groups() == [{"id": 2}]
    assert collector.get_hosts_count() == 44
    assert collector.get_inventories_summary() == {"inventory_count": 5}
    assert collector.get_job_templates_summary()["avg_forks"] == 5
    assert collector.get_settings_jobs() == {"max_forks": 7, "job_retention_days": 14}

    day_stats = collector._fetch_day_stats("2026-05-17T00:00:00", "2026-05-18T00:00:00")
    assert day_stats["total_count"] == 2
    assert len(day_stats["sampled_jobs"]) == 2

    assert (
        collector._detect_peak_pattern({h: (100 if h < 4 else 0) for h in range(24)})
        == "batch_window"
    )
    assert (
        collector._detect_peak_pattern({h: (10 if 8 <= h < 18 else 1) for h in range(24)})
        == "business_hours"
    )
    assert collector._detect_peak_pattern(dict.fromkeys(range(24), 1)) == "distributed_24x7"
    assert collector._detect_allowed_hours({h: (5 if h < 3 else 0) for h in range(24)}) == 4


def test_dynamic_history_collection_and_sizing(monkeypatch):
    collector = DynamicSizingCollector(base_url="https://aap.example.com", token="secret")

    monkeypatch.setattr(
        collector,
        "_fetch_day_stats",
        lambda start, end: {
            "total_count": 3 if "T00:00:00" in start else 0,
            "sampled_jobs": [
                {"started": "2026-05-18T09:00:00Z", "finished": "2026-05-18T10:00:00Z"},
                {"started": "2026-05-18T11:00:00Z", "finished": "2026-05-18T13:00:00Z"},
            ],
        },
    )
    history = collector.get_job_history_stratified(days=2)
    assert history["playbooks_per_day_peak"] == 3
    assert history["jobs_analyzed"] >= 3
    assert history["peak_pattern"] == "batch_window"

    monkeypatch.setattr(
        collector,
        "_get",
        lambda endpoint, params=None: {
            "version": "2.5",
            "license_info": {"license_type": "enterprise"},
        },
    )
    monkeypatch.setattr(
        collector,
        "get_instances",
        lambda: [{"cpu": 8, "memory": 16 * 1024**3, "node_type": "hybrid"}],
    )
    monkeypatch.setattr(collector, "get_instance_groups", lambda: [{"name": "default"}])
    monkeypatch.setattr(collector, "get_hosts_count", lambda: 50)
    monkeypatch.setattr(collector, "get_inventories_summary", lambda: {"inventory_count": 4})
    monkeypatch.setattr(
        collector,
        "get_job_templates_summary",
        lambda: {
            "template_count": 5,
            "avg_forks": 6,
            "median_forks": 5,
            "max_forks": 10,
            "avg_verbosity": 2,
        },
    )
    monkeypatch.setattr(
        collector, "get_settings_jobs", lambda: {"max_forks": 7, "job_retention_days": 10}
    )
    monkeypatch.setattr(
        collector,
        "get_job_history_stratified",
        lambda days=30: {
            "playbooks_per_day_peak": 10,
            "playbooks_per_day_avg": 8.5,
            "job_duration_hours": 1.5,
            "job_duration_p95_hours": 2.0,
            "peak_pattern": "business_hours",
            "jobs_analyzed": 100,
            "jobs_sampled_for_duration": 20,
            "analysis_days": 10,
            "hourly_distribution": {h: (10 if 8 <= h < 18 else 0) for h in range(24)},
        },
    )
    metrics = collector.collect_all_metrics(history_days=10)
    assert metrics["observed"]["version"] == "2.5"
    assert metrics["observed"]["execution_instances"] == 1
    assert metrics["sizing_inputs"]["managed_hosts"] == 50
    assert metrics["sizing_inputs"]["playbooks_per_day_peak"] == 13
    assert metrics["sizing_inputs"]["allowed_hours_per_day"] == 10

    monkeypatch.setattr(
        "aap_migration.sizing.dynamic.DynamicSizingCollector.collect_all_metrics",
        lambda self, history_days=30: metrics,
    )

    class FakeCalculator:
        def generate_sizing_recommendation(self, sizing_inputs, deployment_target):
            return {
                "components": {
                    "platform_gateway": {
                        "replicas": 1,
                        "cpu_per_pod": 1,
                        "memory_per_pod_gb": 2,
                        "total_cpu": 1,
                        "total_memory_gb": 2,
                    },
                    "automation_controller_control_plane": {
                        "controller_pods": 1,
                        "cpu_per_pod": 1,
                        "memory_per_pod_gb": 2,
                        "total_cpu": 1,
                        "total_memory_gb": 2,
                    },
                    "automation_controller_execution_plane": {
                        "execution_pods": 1,
                        "cpu_per_pod": 1,
                        "memory_per_pod_gb": 2,
                        "total_cpu": 1,
                        "total_memory_gb": 2,
                    },
                    "automation_hub": {
                        "replicas": 1,
                        "cpu_per_pod": 1,
                        "memory_per_pod_gb": 2,
                        "total_cpu": 1,
                        "total_memory_gb": 2,
                    },
                    "event_driven_ansible": {
                        "replicas": 1,
                        "cpu_per_pod": 1,
                        "memory_per_pod_gb": 2,
                        "total_cpu": 1,
                        "total_memory_gb": 2,
                    },
                    "database": {"cpu": 1, "memory_gb": 2, "storage_gb": 10},
                    "redis": {"total_cpu": 0.1, "total_memory_gb": 0.2},
                }
            }

    monkeypatch.setattr("aap_migration.sizing.dynamic.AAP26SizingCalculator", FakeCalculator)
    result = calculate_dynamic_sizing(
        base_url="https://aap.example.com",
        token="secret",
        api_prefix="/api/v2",
        history_days=10,
        deployment_target="ocp",
    )
    assert result["mode"] == "dynamic"
    assert result["recommendation"]["components"]["database"]["cpu"] == 4
    assert result["recommendation"]["components"]["platform_gateway"]["cpu_per_pod"] == 2

    recommendation = {
        "components": {
            "automation_controller_execution_plane": {
                "execution_pods": 2,
                "cpu_per_pod": 1,
                "memory_per_pod_gb": 1,
                "total_cpu": 1,
                "total_memory_gb": 1,
            }
        }
    }
    _enforce_minimums(recommendation, deployment_target="containerized")
    assert recommendation["components"]["automation_controller_execution_plane"]["cpu_per_pod"] == 4
    assert recommendation["components"]["automation_controller_execution_plane"]["total_cpu"] == 8
