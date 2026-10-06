"""Artifact serving + cancel contract tests (#4, #11, #12, #13).

Covers the jobs router guards with real job directories: traversal
confinement, secret-sidecar exclusion, and the size cap on downloads
(P1 #4); the single-open capped read boundary (P2 #11); the cancel /
re-poll contract (P2 #12); and offset/cursor pagination over a live
directory (P2 #13).
"""

from pathlib import Path
from typing import Any

import pytest
from api_shared import _wait


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: Any) -> Any:
    monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
    yield


def _seed_job(client: Any, files: dict[str, str]) -> str:
    """Enqueue a job whose worker plants *files* then succeeds; return id."""
    from aap_migration.api.jobs import get_job_manager

    def _plant(job: Any) -> Any:
        base = Path(job["job_dir"])
        for rel, text in files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return {"message": "seeded"}

    job = get_job_manager().submit("seed", {}, _plant)
    done = _wait(client, job["job_id"])
    assert done["status"] == "succeeded", done
    return str(job["job_id"])


class TestArtifactGuards:
    """P1 #4: traversal, sidecars, caps, and media types are route-tested."""

    def test_listing_hides_secret_sidecars(self, client: Any) -> None:
        seeded = _seed_job(
            client,
            {
                "reports/r.md": "# report",
                "config.yaml": "token: x",
                "migration_state.db": "binary",
                "api_fernet.key": "key",
            },
        )
        body = client.get(f"/api/v1/jobs/{seeded}/artifacts").json()
        assert "reports/r.md" in body["artifacts"]
        for hidden in ("config.yaml", "migration_state.db", "api_fernet.key"):
            assert all(hidden not in item for item in body["artifacts"]), body

    def test_sidecar_download_is_404(self, client: Any) -> None:
        seeded = _seed_job(client, {"config.yaml": "token: x"})
        resp = client.get(f"/api/v1/jobs/{seeded}/artifacts/config.yaml")
        assert resp.status_code == 404

    def test_absolute_outside_is_400(self, client: Any) -> None:
        seeded = _seed_job(client, {"reports/r.md": "# report"})
        resp = client.get(f"/api/v1/jobs/{seeded}/artifacts//etc/hostname")
        assert resp.status_code == 400

    def test_download_round_trip_with_media_type(self, client: Any) -> None:
        seeded = _seed_job(client, {"reports/data.json": '{"ok": true}'})
        resp = client.get(f"/api/v1/jobs/{seeded}/artifacts/reports/data.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        assert resp.json() == {"ok": True}

    def test_missing_artifact_is_404(self, client: Any) -> None:
        seeded = _seed_job(client, {"reports/r.md": "# report"})
        resp = client.get(f"/api/v1/jobs/{seeded}/artifacts/reports/nope.md")
        assert resp.status_code == 404


class TestDownloadCap:
    """P2 #11: the size gate is pinned at the boundary (single-open read)."""

    def test_at_cap_downloads_over_cap_413s(self, client: Any, monkeypatch: Any) -> None:
        import aap_migration.api.routers.jobs as jobs_router

        monkeypatch.setattr(jobs_router, "_MAX_ARTIFACT_DOWNLOAD_BYTES", 10)
        seeded = _seed_job(client, {"ten.bin": "0123456789", "eleven.bin": "01234567890"})
        ok_resp = client.get(f"/api/v1/jobs/{seeded}/artifacts/ten.bin")
        assert ok_resp.status_code == 200
        assert ok_resp.content == b"0123456789"
        big_resp = client.get(f"/api/v1/jobs/{seeded}/artifacts/eleven.bin")
        assert big_resp.status_code == 413


class TestArtifactPaging:
    """P2 #13: offset pages plus the exclusive cursor cover a live dir."""

    def _seed_five(self, client: Any) -> str:
        return _seed_job(client, {f"b{i}.txt": f"file {i}" for i in range(1, 6)})

    def _full(self, client: Any, seeded: str) -> list[str]:
        body = client.get(f"/api/v1/jobs/{seeded}/artifacts", params={"limit": 100}).json()
        assert body["truncated"] is False
        return list(body["artifacts"])

    def test_offset_pages_and_exact_boundary(self, client: Any) -> None:
        seeded = self._seed_five(client)
        full = self._full(client, seeded)
        assert [p for p in full if p.startswith("b")] == [f"b{i}.txt" for i in range(1, 6)]
        total = len(full)
        first = client.get(
            f"/api/v1/jobs/{seeded}/artifacts", params={"limit": 2, "offset": 0}
        ).json()
        assert first["artifacts"] == full[0:2]
        assert first["total"] == total
        assert first["truncated"] is True
        # Exact boundary: total == offset + limit + 1 hides one item.
        edge = client.get(
            f"/api/v1/jobs/{seeded}/artifacts",
            params={"limit": 2, "offset": total - 3},
        ).json()
        assert edge["artifacts"] == full[total - 3 : total - 1]
        assert edge["truncated"] is True
        last = client.get(
            f"/api/v1/jobs/{seeded}/artifacts",
            params={"limit": 2, "offset": total - 1},
        ).json()
        assert last["artifacts"] == full[total - 1 :]
        assert last["truncated"] is False

    def test_cursor_walks_full_list_without_dups(self, client: Any) -> None:
        seeded = self._seed_five(client)
        full = self._full(client, seeded)
        seen: list[str] = []
        after: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 2}
            if after is not None:
                params["after"] = after
            page = client.get(f"/api/v1/jobs/{seeded}/artifacts", params=params).json()
            seen.extend(page["artifacts"])
            if not page["truncated"]:
                break
            after = page["artifacts"][-1]
        assert seen == full

    def test_cursor_past_end_is_empty(self, client: Any) -> None:
        seeded = self._seed_five(client)
        page = client.get(
            f"/api/v1/jobs/{seeded}/artifacts",
            params={"limit": 2, "after": "zzz-no-such-file"},
        ).json()
        assert page["artifacts"] == []
        assert page["truncated"] is False


class TestCancelContract:
    """P2 #12: cancel of queued/running/terminal/unknown jobs."""

    def test_cancel_lifecycle(self, client: Any) -> None:
        import threading
        import time

        from aap_migration.api.jobs import get_job_manager

        gate = threading.Event()

        def _block(job: Any) -> Any:
            assert gate.wait(30)
            return {"message": "released"}

        running = get_job_manager().submit("blocker", {}, _block)
        deadline = time.time() + 10
        while get_job_manager().get(running["job_id"])["status"] != "running":
            assert time.time() < deadline
            time.sleep(0.05)
        try:
            queued = get_job_manager().submit("queued", {}, lambda j: {"message": "q"})
            # Queued jobs transition immediately.
            cancelled = client.post(f"/api/v1/jobs/{queued['job_id']}/cancel").json()
            assert cancelled["status"] == "cancelled"
            assert cancelled.get("cancel_pending") is False
            # Running jobs stay running with cancel_pending until re-poll.
            pending = client.post(f"/api/v1/jobs/{running['job_id']}/cancel").json()
            assert pending["status"] == "running"
            assert pending["cancel_pending"] is True
        finally:
            gate.set()
        done = _wait(client, running["job_id"])
        assert done["status"] == "cancelled"
        # Terminal jobs refuse with 409; unknown ids 404.
        assert client.post(f"/api/v1/jobs/{running['job_id']}/cancel").status_code == 409
        assert client.post("/api/v1/jobs/does-not-exist/cancel").status_code == 404
