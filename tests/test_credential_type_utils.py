from __future__ import annotations

import pytest

from aap_migration.migration.credential_type_utils import (
    BUILTIN_CREDENTIAL_TYPE_MAX_ID,
    is_builtin_credential_type_id,
    map_managed_credential_types,
)


def test_is_builtin_credential_type_id() -> None:
    assert is_builtin_credential_type_id(1) is True
    assert is_builtin_credential_type_id(2) is True
    assert is_builtin_credential_type_id(BUILTIN_CREDENTIAL_TYPE_MAX_ID) is True
    assert is_builtin_credential_type_id(BUILTIN_CREDENTIAL_TYPE_MAX_ID + 1) is False
    assert is_builtin_credential_type_id("2") is True
    assert is_builtin_credential_type_id(None) is False
    assert is_builtin_credential_type_id("not-an-id") is False


@pytest.mark.asyncio
async def test_map_managed_credential_types_by_name() -> None:
    class FakeClient:
        def __init__(self, results):
            self.results = results

        async def get(self, endpoint, params=None):
            return {"results": self.results}

    mapped: list[dict] = []

    class FakeState:
        def create_or_update_mapping(self, **kwargs):
            mapped.append(kwargs)

    count = await map_managed_credential_types(
        FakeClient([{"id": 2, "name": "Source Control"}, {"id": 3, "name": "Missing"}]),
        FakeClient([{"id": 102, "name": "Source Control"}]),
        FakeState(),  # type: ignore[arg-type]
    )

    assert count == 1
    assert mapped == [
        {
            "resource_type": "credential_types",
            "source_id": 2,
            "target_id": 102,
            "source_name": "Source Control",
        }
    ]
