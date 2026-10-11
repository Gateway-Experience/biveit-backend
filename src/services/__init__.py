# The service layer -- business logic, agnostic of who's calling. A service never knows it's behind
# HTTP (no Request, no status codes, no FastAPI imports): it raises domain exceptions and the
# controller maps them to a protocol. That's what lets the same logic serve a queue consumer or a
# script later.
#
# It's also agnostic of the STORE: services call repositories in storage-neutral terms and never
# import asyncpg or write SQL. A db-shaped detail leaking into a service signature is a smell.
#
# A feature with several moving parts gets its own sub-package (e.g. users/), exposing its interface
# through its __init__; anything not clearly part of one stays at this top level.
#
# Not everything needs a service. A pass-through that just forwards to one repo call earns nothing --
# let the controller use the repo directly, and add a service when there's actual logic to hold.
