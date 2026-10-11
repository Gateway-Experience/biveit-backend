# Data contracts. Two kinds live here:
#   - INTERFACES (TypedDict) over the dicts repositories return -- pure shape, no validation, a plain
#     dict at runtime. They document what a table's rows carry so callers can read by key.
#   - REQUEST/RESPONSE CONTRACTS (Pydantic) for untrusted HTTP input, which DO validate.
# Per the typing convention: type the official entities and the API boundary, not every internal
# function.
