# Intentions: what every registrar client (namecom.py today; inet.py for .vn, openprovider.py as a fallback
# later) has in common, so the rest of the app can treat them alike. A client is just a module exposing the
# functions below, in the shapes below -- no base class. domains.py picks the client for a domain (by TLD, see
# the module's constants.REGISTRAR_BY_TLD, or by the registrar a stored domain records) and only ever talks to it
# through these.
#
# The shared shapes are Name.com's (camelCase, as their API names them): it was the first client, so a new one
# translates its registrar's answers into them rather than everything being re-shaped around a second example.
#
# The functions (all async unless noted; see namecom.py for each one's exact shape):
#   is_live() -> bool                            [sync] pointed at production rather than a sandbox
#   get_account_name() -> str                    [sync] which of our accounts it acts as
#   check_availability(domain_names) -> [result]  {"domainName", "purchasable", "purchaseType", "premium",
#                                                 "purchasePrice", "renewalPrice"} -- prices for the min term
#   search(keyword, tlds) -> [result]            name ideas, same result shape; a client without one returns []
#   get_pricing(domain_name, years) -> {"premium", "purchasePrice", "renewalPrice", "transferPrice"} for that
#                                                 term; a term the TLD doesn't allow raises RegistrarError(400)
#   get_min_years(domain_name) -> int            the TLD's shortest registration term (callers cache it)
#   create_domain(domain_name, years, contacts, purchase_price, idempotency_key)
#                                                 -> {"domain": <domain>, "order", "totalPaid"}   SPENDS
#   renew_domain(domain_name, years, purchase_price) -> same shape                                   SPENDS
#   get_domain(domain_name) / list_domains(page) / update_domain(domain_name, changes)
#   set_nameservers(domain_name, nameservers) / get_auth_code(domain_name)
#   list_records / create_record / update_record / delete_record   ({"id", "host", "type", "answer", "ttl"})
#   list_unverified_contacts() / resend_verification(verification_id)
#   get_balance() -> float                        our prepaid USD balance there
# Contacts are {"registrant": {...}, "admin": ..., "tech": ..., "billing": ...}, each in Name.com's contact
# shape (firstName, lastName, companyName, address1, city, state, zip, country, email, phone as E.164).
# A registrar without some of these (no DNS hosting, say) raises RegistrarError(501, ...) for them.


class RegistrarError(Exception):
    """A registrar refused or failed a call. `registrar` is which one ("namecom"), `status` the HTTP-style status
    (400 = invalid input, 404 = not ours, 409 = not allowed right now, 429 = rate limited), `message` their
    reason, as they put it."""

    def __init__(self, registrar: str, status: int, message: str):
        super().__init__(f"{registrar} {status}: {message}")
        self.registrar = registrar
        self.status = status
        self.message = message


class LiveSpendRefused(Exception):
    """A call that spends money was about to hit a registrar's live API from outside prod. Every client guards
    its spending calls this way, so a dev box with live credentials can read but never buy."""
