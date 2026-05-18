from __future__ import annotations

import pytest

from aap_migration.sizing.calculator import AAP26SizingCalculator, main
from aap_migration.sizing.dynamic import (
    DynamicSizingCollector,
    _enforce_minimums,
    _OnlineStats,
    _ReservoirSampler,
    calculate_dynamic_sizing,
)


def sizing_metrics(**overrides):
    metrics = {
        "managed_hosts": 2000,
        "playbooks_per_day_peak": 4000,
        "tasks_per_job": 40,
        "job_duration_hours": 0.5,
        "allowed_hours_per_day": 8,
        "peak_pattern": "business_hours",
        "job_retention_hours": 72,
        "forks_observed": 12,
        "num_controllers": 3,
        "num_hub_nodes": 1,
        "hub_cpu_percent": 55,
        "hub_memory_percent": 25,
        "database_vcpu": 12,
        "database_memory_gb": 64,
        "database_cpu_percent": 60,
        "database_memory_percent": 40,
        "verbosity_level": 2,
    }
    metrics.update(overrides)
    return metrics


def test_sizing_calculator_methods_and_recommendations(monkeypatch: pytest.MonkeyPatch) -> None:
    calculator = AAP26SizingCalculator()

    assert calculator.validate_input("managed_hosts", 2_000_000)
    assert calculator.validate_input("managed_hosts", 50) == [
        "ℹ️ managed_hosts value (50) is outside typical range (100-100000). This may indicate unusual workload. "
    ]

    execution = calculator.calculate_execution_node_resources(sizing_metrics())
    controller = calculator.calculate_controller_resources(sizing_metrics())
    database = calculator.calculate_database_resources(sizing_metrics())
    hub = calculator.calculate_automation_hub_resources(sizing_metrics())
    gateway = calculator.calculate_gateway_resources(sizing_metrics())
    eda = calculator.calculate_eda_resources(sizing_metrics())
    redis_enterprise = calculator.calculate_redis_resources(sizing_metrics(managed_hosts=20001))
    redis_small = calculator.calculate_redis_resources(sizing_metrics(managed_hosts=200))

    assert execution["execution_pods"] >= 2
    assert controller["control_plane_pods"] >= 2
    assert database["storage_gb"] >= 60
    assert hub["hub_pods"] >= 2
    assert gateway["gateway_pods"] in {2, 3}
    assert eda["eda_pods"] == 2
    assert redis_enterprise["type"] == "clustered"
    assert redis_small["type"] == "standalone"
    assert calculator.calculate_execution_forks(100, 2, 0.5, peak_pattern="batch_window") > 4
    assert calculator.calculate_execution_memory(100, 2) > 0
    assert calculator.calculate_execution_cpu_avg(100, 2) > 0
    assert calculator.calculate_event_forks(100, 2, 10, 0.5, verbosity_level=4) > 0
    assert calculator.calculate_control_memory_for_events(100, 2) > 0
    assert calculator.calculate_control_cpu_for_events_avg(100, 2) > 0
    assert calculator.calculate_control_memory_for_jobs(50, 2) > 0
    assert calculator.calculate_control_cpu_for_jobs_avg(50, 2) > 0
    assert calculator.calculate_database_storage(100, 2, 10, 7)["total_gb"] > 0

    ocp = calculator.generate_sizing_recommendation(sizing_metrics(), "ocp")
    containerized = calculator.generate_sizing_recommendation(
        sizing_metrics(managed_hosts=50_000), "containerized"
    )

    assert ocp["deployment"]["target"] == "ocp"
    assert containerized["deployment"]["target"] == "containerized"
    assert containerized["components"]["redis"]["type"] == "colocated_ha"
    assert any(
        "official Red Hat Excel reference formulas" in note for note in ocp["deployment_notes"]
    )
    assert ocp["summary"]["total_cpu"] > 0
    assert calculator.validate_results(
        {"total_memory_gb": 10, "execution_pods": 1, "forks_needed": 150},
        {"total_memory_gb": 30, "events_per_task": 50, "verbosity_level": 4},
        {"storage_gb": 50},
    )

    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: None)
    main()


def test_sizing_topology_and_minimum_enforcement() -> None:
    calculator = AAP26SizingCalculator()
    execution = {"forks_needed": 1200, "execution_pods": 6, "total_cpu": 48, "total_memory_gb": 192}
    controller = {"control_plane_pods": 3}
    database = {"storage_gb": 250}

    ocp_topology = calculator._recommend_topology(
        "ocp", execution, controller, database, sizing_metrics()
    )
    container_topology = calculator._recommend_topology(
        "containerized",
        execution,
        controller,
        database,
        sizing_metrics(num_controllers=4, managed_hosts=100_000),
    )

    assert ocp_topology["recommended_topology"] == "enterprise"
    assert container_topology["recommended_topology"] == "enterprise"
    assert "enterprise_reasons" in ocp_topology
    assert "vm_layout" in container_topology

    recommendation = {
        "components": {
            "platform_gateway": {
                "gateway_pods": 2,
                "cpu_per_pod": 1,
                "memory_per_pod_gb": 1,
                "total_cpu": 1,
                "total_memory_gb": 1,
            },
            "automation_controller_control_plane": {
                "control_plane_pods": 2,
                "cpu_per_pod": 1,
                "memory_per_pod_gb": 1,
                "total_cpu": 1,
                "total_memory_gb": 1,
            },
            "automation_controller_execution_plane": {
                "execution_pods": 2,
                "cpu_per_pod": 1,
                "memory_per_pod_gb": 1,
                "total_cpu": 1,
                "total_memory_gb": 1,
            },
            "automation_hub": {
                "hub_pods": 2,
                "cpu_per_pod": 1,
                "memory_per_pod_gb": 1,
                "total_cpu": 1,
                "total_memory_gb": 1,
            },
            "event_driven_ansible": {
                "eda_pods": 2,
                "cpu_per_pod": 1,
                "memory_per_pod_gb": 1,
                "total_cpu": 1,
                "total_memory_gb": 1,
            },
            "database": {"cpu": 1, "memory_gb": 1, "storage_gb": 1},
            "redis": {"total_cpu": 0, "total_memory_gb": 0},
        }
    }
    _enforce_minimums(recommendation, "containerized")

    assert recommendation["components"]["database"]["storage_gb"] == 60
    assert recommendation["components"]["platform_gateway"]["cpu_per_pod"] == 4
    assert recommendation["components"]["platform_gateway"]["total_cpu"] >= 8


def test_dynamic_sizing_collector_and_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = _OnlineStats()
    stats.update(1.0)
    stats.update(3.0)
    sampler = _ReservoirSampler(k=2)
    sampler.add(1.0)
    sampler.add(2.0)
    sampler.add(3.0)
    assert round(stats.mean, 1) == 2.0
    assert stats.stddev > 0
    assert sampler.count == 3
    assert sampler.percentile(0.5) >= 1.0

    collector = DynamicSizingCollector("https://source.example.com", "token", api_prefix="/api/v2")

    def fake_get(endpoint: str, params=None):
        if endpoint == "config/":
            return {"version": "2.4.9", "license_info": {"license_type": "enterprise"}}
        if endpoint == "hosts/":
            return {"count": 500}
        if endpoint == "inventories/":
            return {"count": 20}
        if endpoint == "settings/jobs/":
            return {"DEFAULT_JOB_FORKS": 10, "DAYS_TO_KEEP_LAST_JOB": 14}
        raise RuntimeError("unexpected")

    monkeypatch.setattr(collector, "_get", fake_get)
    monkeypatch.setattr(
        collector,
        "get_instances",
        lambda: [{"node_type": "control", "cpu": 4, "memory": 8 * 1024**3}],
    )
    monkeypatch.setattr(collector, "get_instance_groups", lambda: [{"name": "default"}])
    monkeypatch.setattr(
        collector,
        "get_job_templates_summary",
        lambda: {"template_count": 4, "avg_forks": 8, "max_forks": 12, "avg_verbosity": 2},
    )
    monkeypatch.setattr(
        collector,
        "get_job_history_stratified",
        lambda days=30: {
            "playbooks_per_day_peak": 100,
            "playbooks_per_day_avg": 80,
            "job_duration_hours": 0.4,
            "job_duration_p95_hours": 0.8,
            "peak_pattern": "business_hours",
            "jobs_analyzed": 3000,
            "jobs_sampled_for_duration": 600,
            "analysis_days": days,
            "daily_counts": {"2024-01-01": 100},
            "hourly_distribution": {hour: (10 if 8 <= hour <= 16 else 0) for hour in range(24)},
        },
    )

    collected = collector.collect_all_metrics(history_days=14)
    assert collected["observed"]["managed_hosts"] == 500
    assert collected["sizing_inputs"]["playbooks_per_day_peak"] == 125
    assert (
        collector._detect_peak_pattern({hour: (10 if 8 <= hour <= 16 else 0) for hour in range(24)})
        == "business_hours"
    )
    assert (
        collector._detect_allowed_hours({hour: (5 if hour < 6 else 0) for hour in range(24)}) >= 4
    )

    monkeypatch.setattr(
        "aap_migration.sizing.dynamic.DynamicSizingCollector.collect_all_metrics",
        lambda self, history_days=30: collected,
    )
    dynamic = calculate_dynamic_sizing(
        "https://source.example.com",
        "token",
        api_prefix="/api/v2",
        history_days=14,
        deployment_target="ocp",
    )

    assert dynamic["mode"] == "dynamic"
    assert dynamic["source_observed"]["version"] == "2.4.9"
    assert dynamic["recommendation"]["deployment"]["target"] == "ocp"
