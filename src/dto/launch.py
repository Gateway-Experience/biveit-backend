# Launch typing (services/launch, controllers/launch): the request CONTRACTS (Pydantic) validating untrusted HTTP
# bodies of the launch flow -- choosing a plan and a domain, then paying. See docs/plans/launch-flow.md.

import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, StringConstraints, model_validator

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Registrant(BaseModel):
    """Who a domain bought through us is registered to -- the user, its legal owner (the registrar requires every
    field but company). Name.com's contact fields, snake_cased."""

    model_config = {"extra": "forbid"}

    first_name: _Text
    last_name: _Text
    company_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    address1: _Text
    city: _Text
    state: _Text
    zip: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    # ISO 3166 alpha-2, upper-cased (the pattern is checked before the upper-casing, so it takes either case).
    country: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{2}$")]
    email: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    ]
    # E.164, as the registrar wants it: + then the country code and number, digits only (+84901234567). Typed with
    # spaces, dots, dashes or brackets is fine -- they're dropped before the check (Name.com's own pattern).
    phone: Annotated[
        str,
        BeforeValidator(lambda value: re.sub(r"[\s.\-()]", "", value) if isinstance(value, str) else value),
        StringConstraints(pattern=r"^\+[1-9]\d{7,14}$"),
    ]


class DomainChoice(BaseModel):
    """The domain part of a launch: buy one through us (`price_per_year` = the price the user was shown, so a change
    since is caught), or use the user's own."""

    model_config = {"extra": "forbid"}

    kind: Literal["buy", "own"]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=253)]
    price_per_year: int | None = None


class LaunchCheckoutRequest(BaseModel):
    """POST /projects/{id}/launch/checkout body. A first launch: a `plan`, and for monthly / yearly a `domain` too.
    Later, with a plan already running: only a `domain` (buying one, or switching to the user's own), with the
    `launch_id` of the launch it finishes when there is one (a domain checkout that was abandoned, or failed)."""

    model_config = {"extra": "forbid"}

    plan: Literal["monthly", "yearly", "one_time"] | None = None
    domain: DomainChoice | None = None
    # Required when buying a domain.
    registrant: Registrant | None = None
    launch_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")] | None = None

    @model_validator(mode="after")
    def _check_parts(self) -> "LaunchCheckoutRequest":
        """The shape rules; what's allowed for THIS project is the service's to check."""
        if self.plan is None and self.domain is None:
            raise ValueError("a plan, a domain, or both")
        if self.plan == "one_time" and self.domain is not None:
            raise ValueError("the one-time purchase comes without a domain")
        if self.domain and self.domain.kind == "buy" and self.registrant is None:
            raise ValueError("buying a domain needs its registrant")
        return self


class DomainCheckRequest(BaseModel):
    """POST /projects/{id}/launch/domains/check body: the user's own domain, as typed."""

    model_config = {"extra": "forbid"}

    domain: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ActivateDomainRequest(BaseModel):
    """POST /projects/{id}/launch/domains/activate body: which of the project's domains to serve the site on."""

    model_config = {"extra": "forbid"}

    domain: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=253)]
