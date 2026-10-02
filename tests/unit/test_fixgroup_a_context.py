"""Fixgroup A: verify_execution_pair fails closed on rotation (P1 #2).

An id switch coinciding with a fingerprint (credential/TLS) rotation must
fail and require resubmit, regardless of ``ids_changed``.
``allow_pair_switch`` still permits id switches with an UNCHANGED
fingerprint. No existing test encoded the buggy behavior (only
submit-time opt-in gating, which is untouched).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def _snap_params(store: Any, context_mod: Any, src_id: str, tgt_id: str) -> dict[str, Any]:
    snap = store.pair_fingerprint(src_id, tgt_id)
    return {
        "source_id": src_id,
        "target_id": tgt_id,
        "_snapshot_source_id": snap["source_id"],
        "_snapshot_target_id": snap["target_id"],
        "_snapshot_fp": snap["fp"],
    }


def test_id_switch_with_rotation_fails_closed(pair: Any, client: TestClient) -> None:
    from aap_migration.api import store
    from aap_migration.api.context import verify_execution_pair

    src, tgt = pair
    params = _snap_params(store, None, src["id"], tgt["id"])
    # Switch to a genuinely different pair (different URL/token -> new fp).
    other_src = client.post(
        "/api/v1/connections",
        json={
            "name": "other-src",
            "kind": "source",
            "url": "https://other-src.example.com/api/v2",
            "token": "different",
        },
    ).json()
    other_tgt = client.post(
        "/api/v1/connections",
        json={
            "name": "other-tgt",
            "kind": "target",
            "url": "https://other-tgt.example.com/api/v2",
            "token": "different",
        },
    ).json()
    params.update(
        {
            "source_id": other_src["id"],
            "target_id": other_tgt["id"],
            "allow_pair_switch": True,
        }
    )
    with pytest.raises(ValueError, match="resubmit"):
        verify_execution_pair(params)


def test_id_switch_with_unchanged_fingerprint_allowed(pair: Any, client: TestClient) -> None:
    from aap_migration.api import store
    from aap_migration.api.context import verify_execution_pair

    src, tgt = pair
    snap = store.pair_fingerprint(src["id"], tgt["id"])
    # Clone records with identical URL/token/posture: same fingerprint,
    # different ids -> legitimate pair switch.
    clone_src = client.post(
        "/api/v1/connections",
        json={
            "name": "clone-src",
            "kind": "source",
            "url": "https://src.example.com/api/v2",
            "token": "s3cret",
            "verify_ssl": False,
        },
    ).json()
    clone_tgt = client.post(
        "/api/v1/connections",
        json={
            "name": "clone-tgt",
            "kind": "target",
            "url": "https://tgt.example.com/api/controller/v2",
            "token": "t0p",
            "verify_ssl": False,
        },
    ).json()
    assert store.pair_fingerprint(clone_src["id"], clone_tgt["id"])["fp"] == snap["fp"]
    params: dict[str, Any] = {
        "source_id": clone_src["id"],
        "target_id": clone_tgt["id"],
        "allow_pair_switch": True,
        "_snapshot_source_id": snap["source_id"],
        "_snapshot_target_id": snap["target_id"],
        "_snapshot_fp": snap["fp"],
    }
    verify_execution_pair(params)  # must not raise


def test_pure_rotation_with_opt_in_still_fails(pair: Any, client: TestClient) -> None:
    from aap_migration.api import store
    from aap_migration.api.context import verify_execution_pair

    src, tgt = pair
    params = _snap_params(store, None, src["id"], tgt["id"])
    params["allow_pair_switch"] = True
    new_timeout = 5 if src.get("timeout", 30) != 5 else 60
    store.update_connection(src["id"], timeout=new_timeout)
    with pytest.raises(ValueError, match="resubmit"):
        verify_execution_pair(params)


def test_drift_without_opt_in_still_fails(pair: Any, client: TestClient) -> None:
    from aap_migration.api import store
    from aap_migration.api.context import verify_execution_pair

    src, tgt = pair
    params = _snap_params(store, None, src["id"], tgt["id"])
    verify_execution_pair(params)  # unchanged pair verifies
    store.update_connection(src["id"], url="https://drifted.example.com/api/v2")
    with pytest.raises(ValueError, match="resubmit"):
        verify_execution_pair(params)
