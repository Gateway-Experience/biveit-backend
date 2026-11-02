# Intentions: try the domains module's operations against our Name.com SANDBOX account (settings.namecom_sandbox_*). Without --apply it only reads: balance, an availability
# check, and the domains we hold. With --apply it buys a throwaway domain and walks it through everything we
# can do with one -- DNS records, autorenew, lock, auth code, renewal.
#
#   uv run python -m scripts.probe_namecom [--apply]

import argparse
import asyncio
import uuid

from src.modules import domains
from src.utils import http

# Every call goes to the sandbox.
_REGISTRAR = domains.get_sandbox_registrar()

# A made-up registrant (the sandbox doesn't sync with production, so nothing real gets registered), with our
# real admin inbox so the ICANN verification email can be seen arriving.
_SANDBOX_REGISTRANT = {
    "firstName": "Probe",
    "lastName": "Biveit",
    "address1": "1 Test Street",
    "city": "Ho Chi Minh City",
    "state": "HCM",
    "zip": "700000",
    "country": "VN",
    "email": "admin@biveit.com",
    "phone": "+84901234567",
}


async def _walk_a_domain_through(domain_name: str) -> None:
    registered = await domains.register_domain(domain_name, 1, _SANDBOX_REGISTRANT, str(uuid.uuid4()), sandbox=True)
    print("registered:", registered)
    record = await domains.create_record(
        domain_name, {"host": "www", "type": "CNAME", "answer": "example.com"}, _REGISTRAR
    )
    print("records after adding www:", await domains.list_records(domain_name, _REGISTRAR))
    await domains.delete_record(domain_name, record["id"], _REGISTRAR)
    await domains.update_domain(domain_name, {"autorenewEnabled": False}, _REGISTRAR)
    # A fresh registration can't be unlocked for 60 days (ICANN), so only read the code; it comes back locked too.
    print("auth code:", await domains.get_auth_code(domain_name, _REGISTRAR))
    renewed = await domains.renew_domain(domain_name, 1, registrar=_REGISTRAR)
    print("renewed, now expires:", renewed["domain"]["expireDate"])
    domain = await domains.get_domain(domain_name, _REGISTRAR)
    print(
        "final state:",
        {
            key: domain.get(key)
            for key in ["expireDate", "autorenewEnabled", "locked", "transferLockExpiresAt", "nameservers"]
        },
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Name.com through the domains module.")
    parser.add_argument("--apply", action="store_true", help="buy a throwaway domain and manage it (default: reads)")
    args = parser.parse_args()

    await http.connect()
    try:
        print(f"Name.com sandbox as {domains.get_account_name(_REGISTRAR)}")
        print("balance (USD):", await domains.get_balance(_REGISTRAR))
        domain_name = f"biveit-probe-{uuid.uuid4().hex[:10]}.com"
        print("check:", await domains.check_domains(domain_name, [], sandbox=True))
        print(
            "we hold:",
            [domain["domainName"] for domain in (await domains.list_domains(1, _REGISTRAR)).get("domains", [])],
        )
        if args.apply:
            await _walk_a_domain_through(domain_name)
        else:
            print("reads only -- re-run with --apply to buy and manage a throwaway domain")
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
