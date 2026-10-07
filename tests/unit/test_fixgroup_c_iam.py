"""P2 #20: single pair resolution per IAM job."""

from typing import Any


def test_single_resolution_no_double_store_read(tmp_path: Any, monkeypatch: Any) -> None:
    import contextlib
    import types

    import aap_migration.api.services._core as core
    import aap_migration.api.services.iam as iam

    # Real-shaped ctx (config carries source/target): worker must not call
    # the store a second time.
    source_cfg = types.SimpleNamespace(
        url="https://src.example.com/api/v2", token="s", verify_ssl=False, timeout=30
    )
    target_cfg = types.SimpleNamespace(
        url="https://tgt.example.com/api/v2", token="t", verify_ssl=False, timeout=30
    )
    fake_ctx = types.SimpleNamespace(
        config=types.SimpleNamespace(source=source_cfg, target=target_cfg)
    )
    workdir = tmp_path / "job"
    workdir.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def _fake_ctx(job: Any, **kwargs: Any) -> Any:
        yield (fake_ctx, None, workdir, dict(job.get("params", {})))

    monkeypatch.setattr(core, "chained_ctx", _fake_ctx)
    # iam imports chained_ctx function-locally from _core, so patching core
    # covers production path (no module attr to patch on iam).

    calls: list = []

    def _counting_connections(params: Any, need: Any = "source") -> Any:
        calls.append((dict(params), need))
        return ({"url": "u", "token": "t"}, None)

    monkeypatch.setattr(iam, "_iam_connections", _counting_connections)
    seen: dict = {}
    monkeypatch.setattr(
        iam, "_run_iam_audit", lambda pd, wd, s: seen.update(source=s) or {"message": "ok"}
    )
    out = iam.run_iam_audit({"job_id": "a", "job_dir": str(workdir), "params": {}})
    assert out["message"] == "ok"
    # Single resolution: store re-read never happens with a real ctx.
    assert calls == []
    assert seen["source"]["url"] == "https://src.example.com/api/v2"
