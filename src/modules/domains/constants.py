# The domains module's own constants -- kept here rather than in src/constants.py because the module is meant to
# stand on its own. Registrar-specific ones (API urls...) live in that registrar's client.

# The TLDs a search checks the exact name across, in the order they're shown (a TLD the user typed goes first).
# .vn isn't here: Vietnamese registrants can only get it from a domestic registrar (Decree 147/2024).
SEARCH_TLDS = ["com", "net", "org", "io", "co", "dev", "app", "ai", "xyz"]

# How long a TLD's shortest registration term (found by probing the registrar) stays cached. Registries change
# these about never; a week keeps cold searches rare without pinning a wrong answer for long.
MIN_TERM_CACHE_SECONDS = 7 * 24 * 3600

# Which registrar sells which TLD: a TLD missing here goes to DEFAULT_REGISTRAR. A key can be a full TLD ("com.vn")
# or its last label ("vn", covering every *.vn). Only decides where NEW registrations go -- a domain we hold is
# managed at the registrar that registered it. E.g. "vn": "inet" once there's an iNET client.
DEFAULT_REGISTRAR = "namecom"
REGISTRAR_BY_TLD: dict[str, str] = {}
