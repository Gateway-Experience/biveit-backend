# The controller layer -- the edge. Each module owns one APIRouter: it parses and validates input,
# declares who may call it (Depends on the auth dependencies), delegates to a service, and maps the
# outcome to HTTP. No business logic lives here.
#
# ERROR HANDLING CONVENTION -- this layer is where errors get LOGGED, because it's the outermost point
# an exception reaches before becoming a response. Inner layers raise and let it bubble; the handler
# catches, calls log_http_error(error, request), and returns a status. The one exception is an error a
# service deliberately SWALLOWS -- that one is logged where it's swallowed, since it never bubbles
# here (see services/system_config._refresh_if_stale).
#
# So a handler looks like: try -> delegate -> map known domain exceptions to their status codes ->
# except Exception -> log_http_error + 500. Expected outcomes (a duplicate username, a bad password)
# are NOT logged as errors; they're ordinary 4xx responses.
