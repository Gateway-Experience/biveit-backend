# gTLD/ccTLD Registrar & Reseller APIs for biveit (as of Oct 2026)

Scope note: notes are from public pages only (no accounts created). Several provider sites were unreachable during research (Dynadot docs returned HTTP 522; join.opensrs.com failed TLS), so some rows rely on third-party integration docs (WISECP, WHMCS, Blesta, library READMEs). Those are flagged. Items marked "(training knowledge, unverified)" in Inferences are from the researcher's background knowledge, not a source fetched in this session, and must be verified before relying on them.

## Q1. Which providers have a real reseller API, and of what quality (REST/JSON vs XML, docs, SDKs, webhooks)?

### Takeaway
The cleanest modern REST/JSON reseller APIs are Name.com CORE API (OpenAPI 3.1, sandbox, documented rate limits, no application needed) and Openprovider REST `/v1` (bearer token, OpenAPI, free sandbox signup). CentralNic Reseller (RRPproxy, absorbs Hexonet), OpenSRS and ResellerClub/LogicBoxes are mature wholesale platforms with older-style APIs. Cloudflare's Registrar API (beta since April 2026) is developer-friendly but built for registering your own domains, not for reselling.

### Cited Findings
**Name.com (CORE API)**
- Reseller page: "clean REST endpoints" with an "OpenAPI spec"; "No fees, no application, no sales call"; "Custom rate cards available for high-volume partnerships." A September 2026 product digest covers a redesigned API dashboard. — [Name.com Reseller](https://www.name.com/reseller)
- CORE API is "our latest version". It has a downloadable OpenAPI 3.1 YAML and official SDKs for TypeScript, Go and PHP, with Python "coming soon". It also has a "Reseller MVP Quickstart" (Search → Register → Manage). — [docs.name.com](https://docs.name.com/)
- Endpoints include `POST /core/v1/domains:checkAvailability`, `POST /core/v1/domains:search`, `POST /core/v1/domains:searchStream`, `POST /core/v1/domains` (register), `GET /core/v1/domains/{domain}` and `GET /core/v1/domains/{domain}/records` (DNS). Resource areas include Transfers, Vanity Nameservers and "Webhook Notifications" (no payload details on the overview page). — [Name.com API Overview](https://docs.name.com/docs/api-overview)
- Auth is HTTP Basic with API username + token, HTTPS only. — [Name.com API Overview](https://docs.name.com/docs/api-overview)

**Openprovider**
- REST API v1. Auth is `POST https://api.openprovider.eu/v1/auth/login`, which returns a bearer token. Endpoints include `POST /domains` (register), `POST /domains/check` and `POST /domains/transfer`. You can "Register, transfer, renew, trade and manage" across thousands of TLDs. An OpenAPI spec can be imported or used to generate typed clients (no official SDK is mentioned). — [developer.openprovider.com](https://developer.openprovider.com/)
- `/v1beta` is deprecated as of September 2026 and switches off on 30 June 2027. `/v1` "works the same way", so migrating only changes the base URL. New integrations should target `/v1`. — [docs.openprovider.com](https://docs.openprovider.com/doc/all)

**Cloudflare Registrar API**
- Beta launched 15 Apr 2026 with search, check and register for a curated set of popular TLDs. WHOIS privacy is on by default, and registration uses the account's default contact and payment method. — [Cloudflare changelog](https://developers.cloudflare.com/changelog/post/2026-04-15-registrar-api-beta/); [Domain Name Wire](https://domainnamewire.com/2026/04/15/cloudflare-launches-domain-registration-api/)
- 30 Sep 2026 update: transfers were added, plus a sandbox and an extensions endpoint covering "420+ extensions". It is available via Cloudflare MCP and a `cf` CLI (`cf registrar registrations create example.com`). Domains are sold at cost. The post does not mention platforms, multi-tenancy, reselling or white-label. — [Cloudflare blog: Simplifying domains](https://blog.cloudflare.com/simplifying-domains/)
- A third-party review from before the Sept update said renewals, transfers and contact updates were not yet supported. Transfers are now added; renewals and contact updates remain unconfirmed. — [domscan review](https://domscan.net/reviews/cloudflare-registrar)

**CentralNic Reseller (RRPproxy / Key-Systems, Team Internet) + Hexonet**
- RRPproxy was rebranded to CentralNic Reseller in Oct 2022. It is operated by Key-Systems GmbH (Team Internet Group). — [ICANNWiki RRPproxy](https://icannwiki.org/RRPproxy)
- API hosts are `api.rrpproxy.net` and `api-ote.rrpproxy.net` (OT&E), with separate OT&E credentials. — [WISECP CentralNic module](https://docs.wisecp.com/de/centralnic)
- Hexonet is merging into CentralNic Reseller for resellers. Hexonet's own mirrors conflict on dates: one says no new development after 30 Dec 2024, the other says the platform is discontinued on 31 Dec 2024. Contact handles change during migration. — [Hexonet migration page](https://www3.hexonet.net/migration-to-centralnic-reseller); [ca.hexonet.net mirror](https://ca.hexonet.net/migration-to-centralnic-reseller)

**OpenSRS (Tucows)**
- The API is reached over HTTPS port 55443. The test environment is Horizon (`horizon.opensrs.net`), production is `rr-n1-tor.opensrs.net`, and sub-users cannot authenticate to the API. (Third-party sources.) — [WISECP OpenSRS](https://docs.wisecp.com/de/opensrs); [Net::OpenSRS](https://metacpan.org/pod/Net::OpenSRS)

**ResellerClub / LogicBoxes**
- HTTP API: GET for reads, POST for writes. The test URL is `test.httpapi.com`. — [WHMCS ResellerClub docs](https://whmcsdocs.devops.cpanel.net/8-13/domains/domain-registrar-modules/resellerclub); [ResellerClub KB: Access and Authentication](https://manage.india.resellerclub.com/kb/node/3188)
- There are community Python clients, e.g. the `resellerclub` PyPI CLI. — [PyPI resellerclub](https://pypi.org/project/resellerclub/0.1.2/)

**Porkbun**
- API v3 (JSON). You register with `POST api.porkbun.com/api/json/v3/domain/create/{domain}`, passing the cost in integer US cents, which must match the `checkDomain` quote. `agreeToTerms` must be "yes". An `Idempotency-Key` header is supported, and there is a monthly spend cap (`MONTHLY_SPEND_LIMIT_EXCEEDED`). Keys can be scoped to IPs/domains (`IP_NOT_ALLOWED`). `getRegistrationRequirements` shows which TLDs are API-registrable; .us/.ca/.eu/.au are website-only. — [Porkbun register guide](https://porkbun.com/llms/guides/register-a-domain); [Porkbun API v3 llms-full](https://porkbun.com/llms-full.txt)

**Dynadot**
- Has a Legacy API and a RESTful API. RESTful calls use secret keys to sign an `x-signature` header, and there are separate sandbox keys. — [Dynadot help #155](https://www.dynadot.com/ko/help/question/155/)

**NameSilo**
- API methods (per a community wrapper) include registerDomain, renewDomain, checkRegisterAvailability, listDomains, getDomainInfo and getPrices. — [python-namesilo docs](https://python-namesilo.readthedocs.io)

**Gandi**
- v5 REST API. Rate limit is 1000 req/min per IP. The paying organization is selected with `sharing_id`. — [Gandi API reference](https://api.gandi.net/docs/reference/); [WISECP Gandi](https://docs.wisecp.com/fr/gandi.md)

**NiceNIC**
- Reseller API v2 covers availability, register, renew, transfer, NS/DNS changes and balance check. It requires upgrading to a reseller tier and whitelisting an IP. — [NiceNIC Reseller API v2](https://nicenic.com/support/NiceNIC-Reseller-API-v2-4763)

**Netim**
- Has a reseller program with an API (REST 1.0 per WISECP), and the API password is separate from the panel password. — [Netim reseller program](https://netim.com/en/reseller-program); [WISECP Netim](https://docs.wisecp.com/de/netim.md)

**Entri**
- "Entri Sell" embeds a purchase modal (`purchaseDomain()`), with checkout handled by a registrar partner (IONOS, Squarespace or GoDaddy). It is commission-based, and "no need to become a registrar". White-label is offered on paid tiers. This is an embed/referral model, not a wholesale API. — [Entri Sell docs](https://developers.entri.com/sell/overview.md); [Entri pricing](https://entri.com/pricing)

### Inferences
- For a Python/FastAPI backend wanting REST/JSON, Name.com CORE and Openprovider `/v1` are the two best fits: both have OpenAPI specs, so a Python client can be generated even though neither ships an official Python SDK yet.
- CentralNic Reseller, OpenSRS and LogicBoxes offer broad ccTLD coverage, but their APIs are command/XML-style (training knowledge, unverified: RRPproxy uses a key=value command API, and OpenSRS uses XML-over-HTTPS with MD5 signatures plus a newer REST layer). Expect more adapter code.
- Retail registrars with APIs (Porkbun, Namecheap, Dynadot, NameSilo, Cloudflare) can technically register domains via API, but they are built for "your own account". They lack sub-accounts and white-label, and some have per-account concurrency or spend caps. They are fine for an MVP but weaker as a long-term wholesale backbone.
- Entri Sell is a different model: the user buys from GoDaddy/IONOS/Squarespace and biveit earns a commission. That conflicts with the requirement that the platform pays and resells. It is only worth considering as a zero-ops fallback.
- Cloudflare: registered domains are locked to Cloudflare nameservers (see Q5), the registrant comes from the account default contact, and the post says nothing about resale. Drop it as a reseller primary unless Cloudflare explicitly allows platform resale.

### Gaps
- Name.com webhook payloads/events were not documented on the overview page.
- No webhook info was found for Openprovider, CentralNic or OpenSRS.
- Dynadot's official API docs were down (HTTP 522), so the current command list could not be verified.
- 101domain's API was not researched (no results surfaced).
- Domainr/Fastly (search-only) was not researched in this session.

## Q2. Sandbox / OT&E environments

### Takeaway
Most serious candidates have a free sandbox. Name.com (`api.dev.name.com`, preloaded credit) and Openprovider (self-signup sandbox) are the easiest to start with. OpenSRS Horizon gives $5,000 test credit. RRPproxy has OT&E with separate credentials. Cloudflare added a sandbox in Sept 2026.

### Cited Findings
- **Name.com:** sandbox is `https://api.dev.name.com` and production is `https://api.name.com`. Sandbox credentials are your username + "-test" plus a sandbox token. It has preloaded testing credit and does not sync with production. — [Name.com API Overview](https://docs.name.com/docs/api-overview)
- **Openprovider:** separate sandbox accounts via self-signup at `https://cp.sandbox.openprovider.nl/signup`. "A sandbox account is its own login. Production keys never reach it." — [developer.openprovider.com](https://developer.openprovider.com/)
- **OpenSRS Horizon:** mirrors Live and gives $5,000 test credit. Test registrations don't hit registries, transfers are unavailable, no email is sent, and billing is unavailable. No IP authorization is needed on Horizon. (Third-party sources plus a support.opensrs.com result.) — [OpenSRS support article](https://support.opensrs.com/en/support/solutions/articles/201000063408); [WISECP OpenSRS](https://docs.wisecp.com/de/opensrs)
- **CentralNic Reseller:** OT&E at `api-ote.rrpproxy.net` with separate credentials. — [WISECP CentralNic](https://docs.wisecp.com/de/centralnic)
- **ResellerClub:** requires a separate demo reseller ID + API key against the test URL. Using live credentials on the test URL acts on LIVE. — [WHMCS ResellerClub](https://whmcsdocs.devops.cpanel.net/8-13/domains/domain-registrar-modules/resellerclub); a Blesta thread reports an accidental real registration — [Blesta forum](https://www.blesta.com/forums/topic/3488-logicboxes-sandbox-not-working)
- **Gandi:** separate sandbox account and key (`api.sandbox.gandi.net`), with a limited extension set starting with .com. — [Gandi Sandbox Guide](https://api.sandbox.gandi.net/docs/sandbox/)
- **Dynadot:** sandbox keys exist, but only commands marked "Support API Sandbox" work. A third-party project says the host is `api-sandbox.dynadot.com` and some commands fail there. — [Dynadot help #155](https://www.dynadot.com/ar/%D9%85%D8%B3%D8%A7%D8%B9%D8%AF%D8%A9/%D8%B3%D8%A4%D8%A7%D9%84/155); [domain-mcp sandbox notes](https://glama.ai/mcp/servers/@joachimBrindeau/domain-mcp/blob/10658f1704bee824af3a4adb82750f49f68df4b4/docs/sandbox.md)
- **Namecheap:** separate sandbox account. Its behavior and pricing are not guaranteed to match production (third-party). — [ncheap crate docs](https://docs.rs/crate/ncheap/0.6.0)
- **Cloudflare:** a sandbox was added in Sept 2026 to "test registrar workflows without purchasing a domain". — [Cloudflare blog](https://blog.cloudflare.com/simplifying-domains/)
- **NameSilo:** sandbox mode exists (per Blesta/wrapper docs). — [Blesta NameSilo](https://docs.blesta.com/integrations/modules/namesilo)

### Inferences
- Name.com and Openprovider let you build and test end-to-end before any commercial commitment, which suits a quick POC.
- ResellerClub's "live credentials on the test URL hit live" behavior is a footgun. Config must strictly pair env and credentials.

### Gaps
- Whether the Openprovider sandbox requires approval after signup is not stated.
- Whether the Porkbun API has a sandbox was not found.
- The Cloudflare sandbox's TLD scope is unknown.

## Q3. Account prerequisites (approval, deposit, min spend, KYC, IP whitelist, Vietnam eligibility, payment methods)

### Takeaway
Name.com advertises no fees and no application. Openprovider uses a paid annual membership (from about €49/$49.99) plus a prepaid balance funded by card, PayPal or wire. OpenSRS charges a one-time activation fee. Namecheap gates the API on 20 domains, $50 balance or $50 spend, plus a mandatory IPv4 whitelist. IP whitelisting is common: Namecheap, ResellerClub, NiceNIC, and OpenSRS production. No source explicitly confirms or denies onboarding a Vietnam-registered company for any provider.

### Cited Findings
- **Name.com:** "No fees, no application, no sales call"; "No subscription costs". — [Name.com Reseller](https://www.name.com/reseller)
- **Openprovider:** at-cost pricing via annual membership, with "eleven fixed-price Membership Plans" scaling with annual volume. Sources give different starting prices: €49 (third-party) vs "$49.99/year" (Openprovider blog). Payments come from a prepaid balance topped up by "cards, PayPal, and wire transfer", or SEPA/credit deposit, and invoice billing is possible. — [Openprovider vs OpenSRS blog](https://www.openprovider.com/blog/reseller-business-news/openprovider-vs-opensrs); [Openprovider Costco blog](https://www.openprovider.com/blog/openprovider-costco); [NiceNIC vs Openprovider](https://nicenic.com/news/Nicenic-vs-Openprovider-How-Domain-Reseller-Models-Really-Differ-in-2025-40994); [fitgap](https://us.fitgap.com/products/openprovider-membership)
- **OpenSRS:** one-time non-refundable reseller activation fee (amount not retrieved). Live funds are needed only when you start real registrations. Production requires IP whitelisting (per a developer sample). — [OpenSRS support](https://support.opensrs.com/support/solutions/articles/201000063061); [WISECP OpenSRS](https://docs.wisecp.com/de/opensrs)
- **Namecheap:** API eligibility is ≥20 domains, OR ≥$50 balance, OR ≥$50 spent (sources disagree on whether the spend is lifetime or the last 2 years). The calling IPv4 must be whitelisted (Profile → Tools → API Access). These are third-party sources. — [ncheap crate](https://docs.rs/crate/ncheap/0.6.0); [mcp-namecheap](https://glama.ai/mcp/servers/johnsorrentino/mcp-namecheap/inspect)
- **ResellerClub:** IP whitelist in Settings > API, up to 3 IPs (more via support), no ranges, takes 30–60 min to propagate. — [ResellerClub KB 3188](https://manage.india.resellerclub.com/kb/node/3188); [WHMCS ResellerClub access errors](https://whmcsdocs.devops.cpanel.net/8-13/troubleshooting/troubleshoot-domains/resellerclub/resellerclub-access-errors)
- **NiceNIC:** must upgrade to a reseller tier and whitelist an IP. NiceNIC is actively "Recruiting Domain Reseller Partners in Vietnam". — [NiceNIC API v2](https://nicenic.com/support/NiceNIC-Reseller-API-v2-4763); [NiceNIC Vietnam recruiting](https://nicenic.com/news/Now-Recruiting-Domain-Reseller-Partners-in-Vietnam-41129)
- **Porkbun:** account email and phone must be verified before the first API registration. There is a monthly spend cap. — [Porkbun register guide](https://porkbun.com/llms/guides/register-a-domain)
- **Gandi:** reseller pricing tiers A–E depend on trailing 12-month spend. E is up to 50% off, and new handles start at A. — [Gandi price rates](https://docs.gandi.net/de/billing/price_rates/index.html); [Gandi reseller benefits](https://docs.gandi.net/en/reseller/reseller_benefits/index.html)

### Inferences
- Biveit's backend will run from fixed server IPs, so IP whitelisting is a minor ops item, not a blocker. It does matter for dev machines: use the sandbox or a fixed egress.
- Openprovider's prepaid card/PayPal/wire funding is the most Vietnam-friendly funding path documented. For Name.com, the funding method (card vs. wire) for a VN company is not documented, so ask sales.
- NiceNIC explicitly recruiting Vietnamese resellers is the strongest public signal of VN-company acceptance among the candidates.

### Gaps
- No provider page states its KYC requirements for business resellers or whether Vietnam is a restricted country. This needs a sales or support inquiry.
- The OpenSRS activation fee amount was not retrieved (join.opensrs.com failed TLS). (Training knowledge, unverified: historically about $95 for OpenSRS.)
- CentralNic Reseller's deposit and minimum requirements were not found.

## Q4. TLD coverage (.dev .app .ai .io .co .xyz etc.) and .vn / .com.vn

### Takeaway
All wholesale candidates cover the required gTLD list, and none surfaced as missing a listed TLD. .ai now has a 2-year minimum term (Identity Digital). For .vn, NiceNIC lists .vn pricing and courts VN resellers. Netim warns that Vietnamese residents must register via a local registrar. Openprovider's .vn/.com.vn support was not confirmed.

### Cited Findings
- **.ai:** registration and renewal are 2–10 years, and transfer requires a 2-year renewal. The manager is Identity Digital. — [Wikipedia .ai](https://en.wikipedia.org/wiki/.ai). The 2-year floor applies since Jan 2025 and to renewals. — [NameSilo blog](https://www.namesilo.com/blog/en/domain-name-search/ai-domain-registry-rules-pricing-and-renewals). LogicBoxes says renewals are 2-year terms only. — [LogicBoxes KB 3501](https://manage.logicboxes.com/kb/node/3501). Gandi's registry contract says 2–10 years. — [Gandi .ai contract](https://v4.gandi.net/contracts/es/tld_ai/pdf/3.0/3)
- **Openprovider:** "thousands of TLDs". — [developer.openprovider.com](https://developer.openprovider.com/)
- **Cloudflare:** 420+ extensions. — [Cloudflare blog](https://blog.cloudflare.com/simplifying-domains/)
- **Porkbun:** some TLDs (.us .ca .eu .au) are not API-registrable. — [Porkbun register guide](https://porkbun.com/llms/guides/register-a-domain)
- **.vn:** NiceNIC sells .vn. — [NiceNIC .vn](https://nicenic.com/domain/vn-domain-registration.php). Netim says "Vietnamese residents must register the domains directly with a local Registrar". — [Netim .vn](https://www.netim.com/domain-name/vn-domain.html). A comparison site lists .vn first-year prices of $168.99 at 101domain vs $429.39 at NiceNIC. — [domainoffer.net](https://domainoffer.net/tld/vn/nicenic)

### Inferences
- The Netim note likely reflects VNNIC policy that VN-resident registrants go through a VNNIC-accredited local registrar. Since biveit's customers are mostly Vietnamese, .vn/.com.vn will almost certainly need a local registrar (separate research track), not these gTLD wholesalers.
- The .ai 2-year minimum must be modeled in the product: the price shown is for 2 years, and renewals come in 2-year blocks. Don't hardcode 1-year terms.

### Gaps
- No per-provider TLD lists were fetched to confirm .dev/.app/.io/.co/.xyz individually (all are standard at major wholesalers; see the training-knowledge caveat).
- Openprovider and CentralNic .vn/.com.vn support is unconfirmed.

## Q5. Pricing model, premium handling, and price examples (.com/.io/.ai)

### Takeaway
Price models fall into three groups. At-cost-plus-membership: Openprovider (and Cloudflare, but at cost for own use). Tiered or volume-negotiated: Name.com custom rate cards, Gandi A–E, ResellerClub/OpenSRS tiers. Retail-with-API: Porkbun, Namecheap, NameSilo, Dynadot. Premium domains generally need you to quote first and echo the price back on create (Cloudflare, Porkbun). Concrete per-TLD wholesale prices were mostly not retrievable from public pages.

### Cited Findings
- **Cloudflare:** at cost. Premium needs an explicit fee acknowledgement, echoing the check price on register. — [Cloudflare changelog](https://developers.cloudflare.com/changelog/post/2026-04-15-registrar-api-beta/)
- **Cloudflare:** domains are locked to Cloudflare nameservers: "Nameserver updates are not allowed for domains registered with Cloudflare". — [Cloudflare community](https://community.cloudflare.com/t/unable-to-replace-default-cloudflare-nameservers/861761); [Cloudflare docs: update nameservers](https://developers.cloudflare.com/dns/nameservers/update-nameservers)
- **Porkbun:** the create call must pass a cost matching the checkDomain quote. — [Porkbun guide](https://porkbun.com/llms/guides/register-a-domain)
- **Openprovider:** at cost with membership. — [Openprovider Costco blog](https://www.openprovider.com/blog/openprovider-costco)
- **Name.com:** custom rate cards for volume. — [Name.com Reseller](https://www.name.com/reseller)
- **NameSilo:** a `getPrices` API exists. .com is $8.99 first year / $10.99 renewal (secondary 2025 source). — [python-namesilo](https://python-namesilo.readthedocs.io); [bilibili comparison](https://www.bilibili.com/opus/1092365386343514115)
- **Gandi:** A–E rate tiers. — [Gandi price rates](https://docs.gandi.net/de/billing/price_rates/index.html)

### Inferences
- The "quote then echo price on create" pattern (Cloudflare, Porkbun) is a good idempotent design to copy in biveit: store the quoted price and pass it through so that premium or price changes fail safely.
- Pure at-cost (Cloudflare/Openprovider) gives the best margin, but Openprovider's membership fee is a fixed cost that only pays back at volume.

### Gaps
- Concrete wholesale .com/.io/.ai prices per provider were not retrieved (they need a logged-in price list or a pricing page fetch). (Training knowledge, unverified: .com wholesale is around $10–11 after the 2024–2025 Verisign increases, .io around $35–50, .ai around $70–90/yr billed 2 years.)
- No source found on which providers expose a wholesale price list via API, beyond NameSilo `getPrices` and Cloudflare's check/extensions responses.

## Q6. Operations exposed, rate limits, and reliability reputation

### Takeaway
Name.com documents concrete limits: 20 rps and 3,000 req/hr account-wide, plus per-domain registration throttling with Retry-After. Gandi allows 1000 req/min/IP. Dynadot has historically allowed only one concurrent request per account (2013–2019 forum posts, stale). Reputation data from Reddit/HN was not gathered in this session.

### Cited Findings
- **Name.com:** 20 req/s and 3,000 req/hr account-wide; 429 on excess. Registrations also have a per account+domain limit, returning 429 with Retry-After. — [Name.com API Overview](https://docs.name.com/docs/api-overview)
- **Gandi:** 1000 req/min per IP. — [Gandi API reference](https://api.gandi.net/docs/reference/)
- **Dynadot:** one request at a time per account; a user reported a 10-minute ban after a restart (2019 posts, stale). — [Dynadot forum: API rate limits](https://www.dynadot.com/forums/api/api-rate-limits-7928); [Dynadot forum: API limit](https://www.dynadot.com/forums/api/api-limit-8050)
- **Cloudflare:** registries "impose request limits" and live lookups draw on "limited upstream capacity". No numbers given. — [Cloudflare blog](https://blog.cloudflare.com/simplifying-domains/)
- **Cloudflare:** registration may return an async workflow to poll. — [Cloudflare changelog](https://developers.cloudflare.com/changelog/post/2026-04-15-registrar-api-beta/)
- **Name.com:** streaming search is available (`domains:searchStream`). — [Name.com API Overview](https://docs.name.com/docs/api-overview)

### Inferences
- Registration should be modeled as async with polling or retry everywhere (Cloudflare does this explicitly, and Name.com throttles per domain). An idempotent order record with a state machine fits all providers.
- For in-product search-as-you-type, use a fast availability endpoint (Name.com searchStream, or cached zone checks) and confirm with a real check before purchase.

### Gaps
- No Reddit/HN/forum reputation data was collected (tool budget). This is worth a follow-up, especially on Name.com CORE stability since it is newer, and on Openprovider support quality.
- Rate limits for Openprovider, CentralNic, OpenSRS, Porkbun and Namecheap were not found.
- How each provider handles ICANN RAA registrant email verification via API was not found for any provider.

## Shortlist / drop list (researcher's synthesis, inference-level)

- **Primary candidates:** Name.com CORE API (fastest start, modern REST, sandbox, documented limits; ask about VN funding/KYC and webhooks) and Openprovider REST `/v1` (at-cost wholesale, card/PayPal/wire prepay, free sandbox; the membership fee is a fixed cost).
- **Broad-coverage/backup:** CentralNic Reseller (RRPproxy, now also home of ex-Hexonet resellers; OT&E available; older API style).
- **Possible VN angle:** NiceNIC (recruiting VN resellers, sells .vn, reseller API v2 with IP whitelist).
- **Drop/deprioritize:**
  - Cloudflare Registrar: NS locked to Cloudflare, registrant is the account default contact, no stated resale or platform permission, beta.
  - Entri Sell: referral/commission model where the user pays GoDaddy/IONOS/Squarespace, not platform-paid resale.
  - Porkbun, Namecheap, NameSilo, Dynadot: retail accounts with APIs and no wholesale or sub-account model. Dynadot also has historic single-concurrency limits, and Namecheap has a spend gate plus IPv4 whitelist.
  - Hexonet: merged into CentralNic Reseller.
  - Gandi: viable API, but reseller pricing starts at retail-like A rates.
- **Not researched (gaps):** 101domain API, Domainr/Fastly search, OpenSRS fee specifics.
