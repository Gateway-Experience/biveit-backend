# Intentions: long-running operations, generically -- a request answered right away with an operation id and carried
# on in the background, every event of it recorded under that id, and its caller polling the id for how it went.
# This only records and reads back: what an event's value holds (its kind, a state, an error...), and how an
# operation's events fold into where it stands -- running, done, failed, timed out -- is the operation's own code.
# Nothing here decides anything.

from uuid import UUID, uuid4

from src.modules.long_running_ops.repositories import long_running_op_events as events_repo


async def start(value: dict) -> UUID:
    """A new operation, `value` its first event. Returns its id. [I/O]"""
    operation_id = uuid4()
    await events_repo.insert(operation_id, value)
    return operation_id


async def record(operation_id: UUID, value: dict) -> None:
    """Something that happened to an operation. [I/O]"""
    await events_repo.insert(operation_id, value)


async def get_events(operation_id: UUID) -> list[dict]:
    """An operation's events, oldest first: [{id, created_at, value}] -- empty for an unknown id. [I/O: reads]"""
    return [
        {"id": event["id"], "created_at": event["created_at"], "value": event["value"]}
        for event in await events_repo.find(operation_id)
    ]
