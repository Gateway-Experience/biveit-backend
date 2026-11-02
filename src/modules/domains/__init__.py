# The domains module -- domain names for users' sites, bought through our registrar reseller accounts (Name.com
# today): checking and suggesting domains with prices, buying them, and managing what we hold (DNS, nameservers,
# locks, renewals, owner verification). Registrar-agnostic: callers never pick a registrar -- the module routes by
# TLD, and new names go to the live registrar or its sandbox as the caller's `sandbox=` says -- but every call on a
# held domain takes an optional `registrar=` (pass the one register_domain returned; that's where it lives).
# Callers import THIS package (`from src.modules import domains`), not the files. services/registrars.py is the
# contract each registrar client follows, services/namecom.py has the Name.com API details.

from src.modules.domains.services.domains import (
    DomainUnavailable,
    InvalidDomainQuery,
    check_domains,
    create_record,
    delete_record,
    get_account_name,
    get_auth_code,
    get_balance,
    get_domain,
    get_domain_pricing,
    get_min_years,
    get_registrar,
    get_sandbox_registrar,
    list_domains,
    list_records,
    list_unverified_contacts,
    register_domain,
    renew_domain,
    resend_verification,
    search_domains,
    set_nameservers,
    suggest_domains,
    update_domain,
    update_record,
)
from src.modules.domains.services.registrars import LiveSpendRefused, RegistrarError

__all__ = [
    "DomainUnavailable",
    "InvalidDomainQuery",
    "LiveSpendRefused",
    "RegistrarError",
    "check_domains",
    "create_record",
    "delete_record",
    "get_account_name",
    "get_auth_code",
    "get_balance",
    "get_domain",
    "get_domain_pricing",
    "get_min_years",
    "get_registrar",
    "get_sandbox_registrar",
    "list_domains",
    "list_records",
    "list_unverified_contacts",
    "register_domain",
    "renew_domain",
    "resend_verification",
    "search_domains",
    "set_nameservers",
    "suggest_domains",
    "update_domain",
    "update_record",
]
