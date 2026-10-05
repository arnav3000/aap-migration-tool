"""Regression tests for P1 #6: batch dicts must not be mutated by import.

``_import_parallel`` (and the sequential batch importers) used
``pop("_source_id", ...)`` on caller-owned dicts, so re-passing the same
batch (retry/resume) mis-keyed state and duplicated resources. These tests
reuse the same batch object twice and assert identical behavior.
"""

import asyncio
import copy
from typing import Any

from aap_migration.config import PerformanceConfig
from aap_migration.migration.importers._job_templates import JobTemplateImporter
from aap_migration.migration.importers.identity import LabelImporter


def _make_importer(cls: Any) -> Any:
    return cls(
        client=None,  # stubbed out below; never touched
        state=None,
        performance_config=PerformanceConfig(),
        resource_mappings=None,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_parallel_reuse_same_batch_twice() -> None:
    """Same list object passed twice keys the same source_ids both times."""
    importer = _make_importer(LabelImporter)
    calls: list[tuple[Any, ...]] = []

    async def fake_import_resource(
        resource_type: Any, source_id: Any, data: Any, **kwargs: Any
    ) -> Any:
        calls.append((resource_type, source_id, sorted(data.keys())))
        return {"id": source_id + 1000, "name": data.get("name")}

    importer.import_resource = fake_import_resource

    batch = [
        {"id": 1, "_source_id": 101, "name": "alpha"},
        {"id": 2, "_source_id": 102, "name": "beta"},
    ]
    snapshot = copy.deepcopy(batch)

    first = _run(importer._import_parallel("labels", batch))
    second = _run(importer._import_parallel("labels", batch))

    # Caller-owned batch is untouched (the old pop removed _source_id).
    assert batch == snapshot
    # Both passes keyed by _source_id and stripped it from the payload.
    assert [c[1] for c in calls[:2]] == [101, 102]
    assert [c[1] for c in calls[2:]] == [101, 102]
    assert all("_source_id" not in keys for _, _, keys in calls)
    assert len(first) == 2 and len(second) == 2


def test_parallel_falls_back_to_id_without_source_key() -> None:
    """Dicts without _source_id still key by id (old pop default preserved)."""
    importer = _make_importer(LabelImporter)
    seen: list[Any] = []

    async def fake_import_resource(
        resource_type: Any, source_id: Any, data: Any, **kwargs: Any
    ) -> Any:
        seen.append(source_id)
        return {"id": source_id, "name": data.get("name")}

    importer.import_resource = fake_import_resource

    batch = [{"id": 7, "name": "no-source-key"}]
    _run(importer._import_parallel("labels", batch))
    _run(importer._import_parallel("labels", batch))
    assert seen == [7, 7]
    assert batch == [{"id": 7, "name": "no-source-key"}]


def test_job_templates_batch_not_mutated() -> None:
    """Sequential importer preserves _source_id/schedules/etc. for retries."""
    importer = _make_importer(JobTemplateImporter)
    payloads: list[dict[Any, Any]] = []

    async def fake_import_resource(
        resource_type: Any, source_id: Any, data: Any, **kwargs: Any
    ) -> Any:
        payloads.append(dict(data))
        return {"id": source_id + 5000, "name": data.get("name")}

    async def fake_post(*args: Any, **kwargs: Any) -> Any:
        return {"id": 999}

    async def fake_assoc(*args: Any, **kwargs: Any) -> Any:
        return None

    importer.import_resource = fake_import_resource
    importer.client = type("C", (), {"post": staticmethod(fake_post)})()
    importer.state = type("S", (), {"get_mapped_id": staticmethod(lambda *a: None)})()
    importer._associate_credentials = fake_assoc

    batch = [
        {
            "id": 11,
            "_source_id": 201,
            "name": "jt-1",
            "credentials": [3],
            "schedules": [{"name": "sched-1"}],
            "survey_spec": {"name": "survey"},
            "notifications": {"success": []},
        }
    ]
    snapshot = copy.deepcopy(batch)

    _run(importer.import_job_templates(batch))

    assert batch == snapshot
    assert payloads and "_source_id" not in payloads[0]
    for consumed in (
        "credentials",
        "schedules",
        "survey_spec",
        "notifications",
    ):
        assert consumed not in payloads[0], consumed
