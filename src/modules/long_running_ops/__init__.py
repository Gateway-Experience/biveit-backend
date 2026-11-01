# The long-running operations module -- for requests that take too long to answer in one go: the request is answered
# right away with an operation id, the work carries on in the background, everything that happens to it is recorded
# under that id (long_running_op_events), and the caller polls the id for how it went.
#
# Generic on purpose: it only records events and reads them back. Each operation's own code decides what its events
# hold and folds them into where it stands (running / done / failed, its deadline...) -- and since an event's value
# means nothing here, polling endpoints belong to the operation's caller too, which checks who may see it. An
# operation's outcome is for telling the user; nothing should decide from it.
# First user: switching a site's domain (modules/deployments).
# Callers import THIS package (`from src.modules import long_running_ops`), not the files.

from src.modules.long_running_ops.services.operations import get_events, record, start

__all__ = ["get_events", "record", "start"]
