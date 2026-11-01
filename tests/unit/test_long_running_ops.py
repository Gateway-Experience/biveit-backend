# Long-running operations (modules/long_running_ops): an operation's events recorded under its id and read back in
# order -- nothing more. The table is faked.

import uuid
from datetime import UTC, datetime

import pytest

from src.modules import long_running_ops
from src.modules.long_running_ops.services import operations


@pytest.fixture
def table(monkeypatch):
    state = {"rows": []}

    async def insert(operation_id, value):
        row = {
            "id": len(state["rows"]) + 1,
            "operation_id": operation_id,
            "created_at": datetime.now(UTC),
            "value": value,
        }
        state["rows"] = [*state["rows"], row]
        return row

    async def find(operation_id):
        return [row for row in state["rows"] if row["operation_id"] == operation_id]

    monkeypatch.setattr(operations.events_repo, "insert", insert)
    monkeypatch.setattr(operations.events_repo, "find", find)
    return state


async def test_an_operations_events_come_back_in_order_under_its_id(table):
    first = await long_running_ops.start({"kind": "example", "step": 1})
    other = await long_running_ops.start({"kind": "example"})
    await long_running_ops.record(first, {"step": 2})
    events = await long_running_ops.get_events(first)
    assert [event["value"] for event in events] == [{"kind": "example", "step": 1}, {"step": 2}]
    assert first != other and len(await long_running_ops.get_events(other)) == 1


async def test_an_unknown_operation_has_no_events(table):
    assert await long_running_ops.get_events(uuid.uuid4()) == []
