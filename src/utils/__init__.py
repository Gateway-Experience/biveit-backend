# Generic, domain-agnostic helpers: connection modules (postgres, redis, http), cross-cutting concerns
# (logging, auth, cache), and small shared utilities (common).
#
# The rule for what belongs here: a util must make sense with NO knowledge of this app's domain. A
# helper that mentions users, configs, or any table by name belongs in the layer that owns it
# (repositories/services), not here.
