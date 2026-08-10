"""Unit tests for migration.runner helper functions."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from aap_migration.api.services.job_service import Job, JobStatus
from aap_migration.migration import runner


def test_resource_display_name_prefers_name_then_username() -> None:
    assert runner._resource_display_name({"name": "Proj"}, 1) == "Proj"
    assert runner._resource_display_name({"username": "alice"}, 2) == "alice"
    assert runner._resource_display_name({}, 99) == "99"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"_skip_reason": "  quota exceeded  "}, "quota exceeded"),
        ({"_already_migrated": True}, "Already migrated in state — update secrets if needed"),
        ({"_skipped": True}, "Matched existing managed resource on target — mapped only"),
        ({"name": "x"}, ""),
        ("not-a-dict", ""),
    ],
)
def test_import_result_detail(result, expected) -> None:
    assert runner._import_result_detail(result) == expected


def test_emit_resource_result_logs_skips_and_failures() -> None:
    events: list[dict] = []
    logs: list[str] = []

    runner._emit_resource_result(
        events.append,
        logs.append,
        phase_num=2,
        name="Demo",
        rtype="projects",
        result="failed",
        detail="boom",
    )
    assert events[0]["_event"] == "resource_result"
    assert events[0]["result"] == "failed"
    assert "Failed projects/Demo: boom" in logs[0]

    runner._emit_resource_result(
        events.append,
        logs.append,
        phase_num=2,
        name="Other",
        rtype="users",
        result="skipped",
    )
    assert "Skipped users/Other" in logs[1]


def test_resource_in_orgs_covers_global_and_scoped_types() -> None:
    assert runner._resource_in_orgs("organizations", {}, 5, [5, 6]) is True
    assert runner._resource_in_orgs("organizations", {}, "bad", [5]) is False
    assert runner._resource_in_orgs("settings", {}, 1, [99]) is True
    assert runner._resource_in_orgs("host_inventory_memberships", {}, 1, [99]) is True
    assert runner._resource_in_orgs("projects", {"organization": 7}, 1, [7]) is True
    assert (
        runner._resource_in_orgs(
            "projects", {"summary_fields": {"organization": {"id": 8}}}, 1, [8]
        )
        is True
    )
    assert runner._resource_in_orgs("projects", {"organization": 1}, 1, [2]) is False
    assert (
        runner._resource_in_orgs(
            "credentials", {"organization": None, "summary_fields": {}}, 1, [2]
        )
        is True
    )


def test_build_source_contexts_creates_per_source_state(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'runner.db'}"
    dest_cfg = SimpleNamespace(url="https://dst", token="d", verify_ssl=True, timeout=30)
    source_configs = [
        {
            "url": "https://src-a",
            "token": "a",
            "verify_ssl": True,
            "timeout": 30,
            "auth_scheme": "Token",
            "source_key": "src-a",
            "connection_name": "Source A",
            "name_prefix": "a_",
            "org_ids": [1, 2],
        }
    ]

    _target_config, target_client, sources = runner._build_source_contexts(
        source_configs,
        dest_cfg,
        "Bearer",
        db_url,
    )

    assert target_client is not None
    assert len(sources) == 1
    assert sources[0]["name_prefix"] == "a_"
    assert sources[0]["org_ids"] == [1, 2]
    assert sources[0]["state"].source_key == "src-a"


@pytest.mark.asyncio
async def test_build_credential_review_ignores_consumer_errors_and_filters_orgs() -> None:
    class FakeClient:
        async def get(self, endpoint, params=None):
            if endpoint == "projects/":
                return {
                    "results": [
                        {"name": "P1", "credential": 100, "organization": 1},
                        {"name": "P2", "credential": 100, "organization": 99},
                    ]
                }
            if endpoint == "organizations/1/galaxy_credentials/":
                return {"results": [{"id": 200}]}
            if endpoint == "instance_groups/":
                raise RuntimeError("instance groups unavailable")
            return {"results": []}

    review = await runner._build_credential_review(
        FakeClient(),
        [
            {
                "name": "Machine",
                "credential_type": "ssh",
                "organization": "Default",
                "source_id": "100",
                "source": "Prod",
                "name_prefix": "prod_",
            },
            {
                "name": "Galaxy",
                "credential_type": "galaxy",
                "organization": "Default",
                "source_id": "200",
                "source": "Prod",
            },
        ],
        [1],
    )

    by_name = {item["name"]: item for item in review}
    assert by_name["Machine"]["used_by"][0]["resource_type"] == "projects"
    assert len(by_name["Machine"]["used_by"]) == 1
    assert by_name["Galaxy"]["used_by"][0]["resource_type"] == "organizations (galaxy)"


@pytest.mark.asyncio
async def test_handle_credential_pause_no_credentials_is_noop() -> None:
    job = Job("job-1", "Pause", "migration")

    class FakeService:
        def persist_job(self, _job) -> None:
            raise AssertionError("should not persist")

    await runner._handle_credential_pause(
        job,
        FakeService(),
        [],
        [{"url": "https://src", "org_ids": [1], "src_client": object()}],
        "plan",
        "phase",
        lambda _event: None,
        lambda _msg: None,
    )


@pytest.mark.asyncio
async def test_handle_credential_pause_unmatched_sources_still_pause() -> None:
    job = Job("job-2", "Pause", "migration")
    job.result = {}

    emitted: list[dict] = []
    logs: list[str] = []

    class FakeService:
        def persist_job(self, stored_job) -> None:
            assert stored_job.status == JobStatus.WAITING_FOR_INPUT

    created = [
        {
            "name": "orphan",
            "credential_type": "Machine",
            "organization": "Default",
            "source_id": "9",
            "source": "https://missing-label",
            "name_prefix": "",
        }
    ]
    sources = [
        {
            "url": "https://src",
            "connection_name": "Different Label",
            "org_ids": [1],
            "src_client": object(),
        }
    ]

    task = asyncio.create_task(
        runner._handle_credential_pause(
            job,
            FakeService(),
            created,
            sources,
            "plan-1",
            "phase-1",
            emitted.append,
            logs.append,
        )
    )
    await asyncio.sleep(0)
    assert job.status == JobStatus.WAITING_FOR_INPUT
    assert emitted[0]["_event"] == "credential_pause"
    assert emitted[0]["credentials"][0]["name"] == "orphan"
    job._resume_event.set()
    await task
    assert job.status == JobStatus.RUNNING
    assert any("Resumed" in line for line in logs)


def test_sort_disk_resource_types_orders_known_types_first() -> None:
    ordered = runner._sort_disk_resource_types(["users", "organizations", "custom_type"])
    assert ordered.index("organizations") < ordered.index("users")
    assert ordered[-1] == "custom_type"


def test_load_export_metadata_reads_json(tmp_path) -> None:
    metadata = {"resource_types": {"hosts": {"count": 3}}}
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    assert runner._load_export_metadata(tmp_path) == metadata


@pytest.mark.asyncio
async def test_migrate_resource_type_fast_path_when_fully_excluded() -> None:
    events: list[dict] = []
    logs: list[str] = []
    sources = [
        {
            "excluded_ids": {"projects": ["1", "2"]},
            "preview_resources": {
                "projects": [{"source_id": 1}, {"source_id": 2}],
            },
        }
    ]

    created, skipped, failed, exported = await runner._migrate_resource_type(
        "projects",
        sources,
        object(),
        4,
        events.append,
        logs.append,
        [],
    )

    assert (created, skipped, failed, exported) == (0, 2, 0, 0)
    assert events[0]["_event"] == "phase_complete"
    assert "excluded by user" in logs[0]


@pytest.mark.asyncio
async def test_run_disk_export_writes_metadata_and_progress(tmp_path, monkeypatch) -> None:
    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def export_all_parallel(self, types, resume=False, progress_callback=None):
            if progress_callback:
                progress_callback("organizations", {"exported": 100})
            return {"organizations": {"exported": 2, "failed": 1}}

    monkeypatch.setattr(
        "aap_migration.migration.parallel_exporter.ParallelExportCoordinator",
        FakeCoordinator,
    )

    logs: list[str] = []
    client = SimpleNamespace(config=SimpleNamespace(url="https://source.example.com"))
    result = await runner.run_disk_export(
        client,
        object(),
        tmp_path,
        resource_types=["organizations"],
        log=logs.append,
    )

    assert result["total_resources"] == 2
    assert result["resource_types"]["organizations"]["count"] == 2
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["total_resources"] == 2
    assert metadata["source_url"] == "https://source.example.com"
    assert any("Export complete" in line for line in logs)


def test_load_export_metadata_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="metadata.json not found"):
        runner._load_export_metadata(tmp_path)


@pytest.mark.asyncio
async def test_build_credential_review_covers_consumer_types_and_org_filter() -> None:
    class FakeClient:
        async def get(self, endpoint, params=None):
            if endpoint == "projects/":
                return {
                    "results": [
                        {"name": "P1", "credential": 100, "organization": 1},
                        {"name": "P2", "credential": 100, "organization": 99},
                        {"name": "P3", "credential": None},
                    ]
                }
            if endpoint == "execution_environments/":
                return {"results": [{"name": "EE", "credential": 300, "organization": 1}]}
            if endpoint == "inventory_sources/":
                return {
                    "results": [
                        {
                            "name": "InvSrc",
                            "credential": 400,
                            "summary_fields": {"organization": {"id": 1}},
                        }
                    ]
                }
            if endpoint == "credential_input_sources/":
                return {"results": [{"name": "CIS", "source_credential": 500}]}
            if endpoint == "instance_groups/":
                return {"results": [{"name": "IG", "credential": 600}]}
            if endpoint == "organizations/2/galaxy_credentials/":
                raise RuntimeError("galaxy unavailable")
            if endpoint == "organizations/1/galaxy_credentials/":
                return {"results": [{"id": 200}]}
            return {"results": []}

    created = [
        {
            "name": "Machine",
            "credential_type": "ssh",
            "organization": "Default",
            "source_id": "100",
        },
        {
            "name": "EE Cred",
            "credential_type": "ssh",
            "organization": "Default",
            "source_id": "300",
        },
        {
            "name": "Inv Cred",
            "credential_type": "scm",
            "organization": "Default",
            "source_id": "400",
        },
        {
            "name": "Input Cred",
            "credential_type": "scm",
            "organization": "Default",
            "source_id": "500",
        },
        {"name": "IG Cred", "credential_type": "ssh", "organization": "", "source_id": "600"},
        {
            "name": "Galaxy",
            "credential_type": "galaxy",
            "organization": "Default",
            "source_id": "200",
        },
    ]

    review = await runner._build_credential_review(FakeClient(), created, [1, 2])
    by_id = {item["name"]: item for item in review}

    assert by_id["Machine"]["used_by"][0]["resource_type"] == "projects"
    assert len(by_id["Machine"]["used_by"]) == 1
    assert by_id["EE Cred"]["used_by"][0]["resource_type"] == "execution_environments"
    assert by_id["Inv Cred"]["used_by"][0]["resource_type"] == "inventory_sources"
    assert by_id["Input Cred"]["used_by"][0]["resource_type"] == "credential_input_sources"
    assert by_id["IG Cred"]["used_by"][0]["resource_type"] == "instance_groups"
    assert by_id["Galaxy"]["used_by"][0]["resource_type"] == "organizations (galaxy)"


@pytest.mark.asyncio
async def test_handle_credential_pause_uses_credential_review_for_matched_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job("job-3", "Pause", "migration")
    job.result = {}

    async def fake_review(_client, created, _org_ids):
        return [{"name": created[0]["name"], "used_by": [], **created[0]}]

    monkeypatch.setattr(runner, "_build_credential_review", fake_review)

    emitted: list[dict] = []
    logs: list[str] = []

    class FakeService:
        def persist_job(self, stored_job) -> None:
            assert stored_job.status == JobStatus.WAITING_FOR_INPUT

    created = [
        {
            "name": "machine",
            "credential_type": "Machine",
            "organization": "Default",
            "source_id": "9",
            "source": "Prod",
            "name_prefix": "prod_",
        }
    ]
    sources = [
        {
            "url": "https://src",
            "connection_name": "Prod",
            "org_ids": [1],
            "src_client": object(),
        }
    ]

    task = asyncio.create_task(
        runner._handle_credential_pause(
            job,
            FakeService(),
            created,
            sources,
            "plan-2",
            "phase-2",
            emitted.append,
            logs.append,
        )
    )
    for _ in range(100):
        if job.status == JobStatus.WAITING_FOR_INPUT:
            break
        await asyncio.sleep(0.01)
    else:
        await task
        pytest.fail("credential pause was not reached")

    assert emitted[0]["credentials"][0]["name"] == "machine"
    job._resume_event.set()
    await task


@pytest.mark.asyncio
async def test_run_cac_org_update_patches_environment_and_galaxy_credentials() -> None:
    events: list[dict] = []
    logs: list[str] = []

    class FakeState:
        def get_mapped_id(self, rtype: str, source_id: int):
            mapping = {
                ("organizations", 1): 101,
                ("execution_environments", 5): 205,
                ("credentials", 9): 909,
            }
            return mapping.get((rtype, source_id))

    class FakeSrcClient:
        async def get(self, endpoint: str):
            if endpoint == "organizations/1/":
                return {"id": 1, "name": "Default", "default_environment": 5}
            if endpoint == "organizations/1/galaxy_credentials/":
                return {"results": [{"id": 9}]}
            raise AssertionError(endpoint)

    class FakeTargetClient:
        def __init__(self) -> None:
            self.updates: list[tuple] = []
            self.posts: list[tuple] = []

        async def update_resource(self, rtype, target_id, patch):
            self.updates.append((rtype, target_id, patch))

        async def post(self, endpoint, data):
            self.posts.append((endpoint, data))

    target = FakeTargetClient()
    sources = [{"src_client": FakeSrcClient(), "state": FakeState(), "org_ids": [1]}]

    updated = await runner._run_cac_org_update(sources, target, 4, events.append, logs.append)

    assert updated == 1
    assert target.updates == [("organizations", 101, {"default_environment": 205})]
    assert target.posts == [("organizations/101/galaxy_credentials/", {"id": 909})]
    assert events[0]["result"] == "updated"


@pytest.mark.asyncio
async def test_run_cac_org_update_skips_unmapped_orgs_and_logs_warnings() -> None:
    logs: list[str] = []

    class FakeState:
        def get_mapped_id(self, *_args):
            return None

    class BrokenSrcClient:
        async def get(self, endpoint: str):
            if endpoint == "organizations/3/":
                raise RuntimeError("org fetch failed")
            return {"id": 3, "name": "Other"}

    sources = [{"src_client": BrokenSrcClient(), "state": FakeState(), "org_ids": [2, 3]}]

    updated = await runner._run_cac_org_update(sources, object(), 1, lambda _e: None, logs.append)

    assert updated == 0
    assert any("CaC org-update for 3" in line for line in logs)


@pytest.mark.asyncio
async def test_run_disk_transform_writes_metadata_and_progress(tmp_path, monkeypatch) -> None:
    in_dir = tmp_path / "exports"
    out_dir = tmp_path / "xformed"
    in_dir.mkdir()
    (in_dir / "metadata.json").write_text(
        json.dumps(
            {
                "resource_types": {"organizations": {"count": 2}},
                "records_per_file": 500,
            }
        )
    )

    class FakeCoordinator:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def transform_all_parallel(self, types, progress_callback=None):
            if progress_callback:
                progress_callback("organizations", {"count": 100})
            return {"organizations": {"count": 2, "failed": 1}}

    monkeypatch.setattr(
        "aap_migration.migration.parallel_transformer.ParallelTransformCoordinator",
        FakeCoordinator,
    )

    logs: list[str] = []
    result = await runner.run_disk_transform(object(), in_dir, out_dir, log=logs.append)

    assert result["total_transformed"] == 2
    assert result["total_failed"] == 1
    metadata = json.loads((out_dir / "metadata.json").read_text())
    assert metadata["total_resources"] == 2
    assert metadata["total_failed"] == 1
    assert any("Transform complete" in line for line in logs)
