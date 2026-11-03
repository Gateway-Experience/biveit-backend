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
    """A domain to buy through us. `price_per_year`: the price the user was shown, so a change since is caught."""

    model_config = {"extra": "forbid"}

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=253)]
    price_per_year: int | None = None


class LaunchCheckoutRequest(BaseModel):
    """POST /projects/{id}/launch/checkout body: ONE thing to pay for -- a `plan` (or the one-time purchase), or,
    with a plan running, a `domain` to buy (with its `registrant`). The provider has no cart, so buying both is two
    checkouts, the plan first."""

    model_config = {"extra": "forbid"}

    plan: Literal["monthly", "yearly", "one_time"] | None = None
    domain: DomainChoice | None = None
    registrant: Registrant | None = None

    @model_validator(mode="after")
    def _check_parts(self) -> "LaunchCheckoutRequest":
        """The shape rules; what's allowed for THIS project is the service's to check."""
        if (self.plan is None) == (self.domain is None):
            raise ValueError("a plan or a domain, one at a time")
        if self.domain is not None and self.registrant is None:
            raise ValueError("buying a domain needs its registrant")
        return self


class DomainCheckRequest(BaseModel):
    """POST /projects/{id}/launch/domains/check and POST /projects/{id}/launch/domains bodies: the user's own
    domain, as typed."""

    model_config = {"extra": "forbid"}

    domain: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class SubdomainRequest(BaseModel):
    """POST /projects/{id}/launch/subdomain body: the slug the user picked for <slug>.<PROD_DOMAIN>, as typed (its
    rules are checked by services/launch/subdomains, which answers with why it can't be used)."""

    model_config = {"extra": "forbid"}

    slug: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=63)]


class ActivateDomainRequest(BaseModel):
    """POST /projects/{id}/launch/domains/activate body: which of the project's domains to serve the site on."""

    model_config = {"extra": "forbid"}

    domain: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=253)]
