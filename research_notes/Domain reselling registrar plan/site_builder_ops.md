# How site builders run in-product domain reselling, and what a domain reseller must do (late 2026)

Research date: 2026-10-23. About 24 tool calls. Search snippets were thin for several platforms. Wherever I fall back on background knowledge without a fetched source, the item is labelled **[unverified/background]** and placed under Inferences or Gaps, never under Cited Findings.

## 1. How the major platforms do it (registrar, registrant, privacy, DNS/TLS, renewal, transfer, refunds)

### Takeaway
There are three patterns. (a) The platform is a **reseller under a wholesale registrar**: Vercel uses name.com (Tucows shows as registrar of record), Wix resells Tucows for most TLDs, and Netlify (historically) resold name.com. (b) The platform is **itself ICANN-accredited**: Squarespace Domains, Hostinger, and Wix for .com/.net. (c) The platform **does not sell at all and hands off to a partner**: Framer sends users to Hover with a voucher. In every pattern the **customer is the registrant**. Buying a domain auto-creates a platform-hosted DNS zone plus automatic Let's Encrypt or wildcard TLS. Renewal is auto-renew on the card on file, with an email warning first.

### Cited Findings
- **Vercel:** On 2025-09-25 Vercel announced "we partnered with name.com as our upstream registrar". It sells "at registrar cost", up to 50% cheaper on popular TLDs, with no upsells, logged-out search that streams results, a premium flag, and bulk checkout. The search speed comes from "structured concurrency, layered caching, Bloom filters, and partitioned batching". — [Vercel changelog](https://vercel.com/changelog/vercel-domains-at-cost-pricing-and-the-fastest-on-the-web)
- name.com's Dec 2025 press release names Vercel, Netlify, Bolt and Replit as API partners, which suggests the AI-builder wave is standardizing on name.com's reseller API. — [BusinessWire](https://www.businesswire.com/news/home/20251209365505/en/name.com-API-Fuels-a-New-Era-of-Web-Building-with-Key-Partnerships); [TheDomains](https://www.thedomains.com/2025/12/09/name-com-partners-with-boltnetlify-and-vercel/)
- A user report says a Vercel-bought domain shows **Tucows Domains Inc** as registrar in WHOIS with Vercel as the "Registration Service Provider". It appears as "Third Party" in the Vercel UI. — [Vercel community](https://community.vercel.com/t/domain-bought-in-vercel-shows-as-third-party-not-first-party/13696)
- A Vercel domain stuck on **clientHold**: name.com support said it is "partner-managed by Vercel" and would not touch the domain or its DNS. This is the classic reseller-customer trap: only the reseller can act. — [Vercel community](https://community.vercel.com/t/my-domain-is-stuck-on-clienthold-name-com-confirmed-partner-managed-by-vercel/49981)
- **Wix:** "Wix.com Ltd. acts as reseller for Tucows Domains Inc." Wix may send a registration to a different registrar than first assigned "due to technical reasons", which is multi-registrar routing. — [Wix–Tucows registration agreement](https://www.wix.com/domains/domain-registration-agreement/tucows)
- Wix says it is an ICANN-accredited registrar for .com/.net and a reseller for the other TLDs. "Wix acts as the registrar of record but ownership sits with you." — [Wix blog](https://www.wix.com/blog/does-wix-own-your-domain)
- A WHOIS record for a Wix-sold .net shows Tucows as registrar and **Contact Privacy Inc.** (Tucows' privacy service) as registrant, so privacy is on by default. — [bgp.he.net record](https://bgp.he.net/dns/junca.net)
- **Netlify:** Buying a domain auto-creates a Netlify DNS zone and provisions a **wildcard certificate**. Registration is for 1 year and auto-renews by default on the team's payment method. Netlify emails the renewal cost before the end of the term. **IDNs are not supported** for purchase. — [Netlify docs: register and buy a domain](https://docs.netlify.com/domains/configure-domains/register-and-buy-a-domain/)
- Netlify's auto Let's Encrypt issuance starts "once the new domain records have propagated". This source is from 2018, so it is stale. — [Netlify blog 2018](https://www.netlify.com/blog/2018/06/19/buy-and-secure-a-custom-domain-through-netlify)
- A 2021 community answer (stale) says Netlify resold name.com, and a lapsed domain might be re-bought directly at name.com. — [Netlify answers](https://answers.netlify.com/t/unable-to-enable-auto-renewal-of-domain/20467)
- **Framer:** "Framer does not sell domains." Instead, Framer issues a voucher, Hover handles registration and checkout on Hover's own site, and the domain is auto-connected to the Framer site. The voucher gives a free or 50% off first year for selected TLDs, on new registrations only, after which standard renewal pricing applies. — [Framer help](https://framer.com/help/articles/is-framer-a-domain-registrar); [Hover support](https://support.hover.com/support/solutions/articles/201000124624-framer-and-hover-custom-domain-partnership); [createwith.com (secondary)](https://www.createwith.com/tool/framer/updates/framer-bundles-free-custom-domain-with-annual-site-plans)
- **Squarespace:** Squarespace acquired all Google Domains registrations and accounts on 2023-09-07, and the migration is complete. Domains auto-renew annually, with an email **15 days before** the charge. Auto-renew can be disabled. — [Squarespace support](https://support.squarespace.com/hc/articles/17131164996365)
- Google's side of the migration: if auto-renew payment fails, the subscription is cancelled. Renewal is then only possible directly at Squarespace and only "if still available". — [Google Workspace help](https://knowledge.workspace.google.com/admin/domains/renew-an-expired-domain-registration)
- **Shopify:** auto-renew can be configured in the admin, but the help pages I fetched contain no detail on registrar, grace periods or refunds. — [Shopify help](https://help.shopify.com/en/manual/domains/managing-domains/renewing-domains)
- Shopify community, renewal failure modes:
  - Renewals fail when the merchant changes cards, and there is no way to retry with the new card.
  - A Shopify staff member says the card issuer may block the domain charge even when the subscription charge succeeds. The fix is a manual renewal by support.
  - After expiry, per a help-doc excerpt quoted in a thread, there are "40 days to contact Shopify support to renew".
  — [community 1](https://community.shopify.com/t/how-can-i-renew-my-domain-after-a-failed-auto-renewal/59899); [community JP](https://community.shopify.com/t/topic/324013); [community 3](https://community.shopify.com/t/how-can-i-renew-an-expired-domain-on-shopify/42728)
- A Shopify thread titled "Domain paid but still pending renewal" shows paid-but-not-renewed drift between billing and the registrar. — [Shopify community](https://community.shopify.com/t/domain-paid-but-still-pending-renewal/412026)
- **Hostinger:** an aggregator site (unverified) says Hostinger is ICANN-accredited (IANA 1636), includes WHOIS privacy free, and prices .com at $9.99 first year and $19.99 renewal, plus a $0.20 ICANN fee. That is the classic first-year-discount, renew-at-retail pattern. — [domainoffer.net](https://domainoffer.net/tld/com/hostinger)

### Inferences
- With an AI-builder audience, the closest analogues to biveit are Vercel, Netlify, Bolt and Replit on the name.com reseller API. Both Vercel and Netlify put the bought domain on **platform nameservers**, which makes DNS and TLS fully automatic. That beats "add these A/CNAME records" for a 2-click flow.
- The Vercel clientHold story is the key product risk. Once biveit is the reseller, the upstream registrar will not support biveit's users. Biveit must own support, a self-serve auth code, and unlock, or users are stranded.
- Frame the first-year price as a promotion and show the renewal price up front. Hostinger-style 2x renewal price is common, but it also drives complaints.

### Gaps
- I could not fetch primary help pages for **Webflow, Carrd, Ghost**, nor for Shopify's registrar, privacy and transfer specifics. **[unverified/background]**:
  - Shopify historically used Tucows/OpenSRS.
  - Webflow, Carrd and Ghost(Pro) do not sell domains in-product (they ask users to connect an external domain), and Ghost has a Namecheap/Cloudflare guide.
  - Verify all of this before citing.
- Refund policies per platform were not found. Most are non-refundable after the 5-day AGP window, but this is unconfirmed.
- Squarespace and Shopify grace and redemption periods are not confirmed beyond "40 days" (Shopify, a quoted excerpt).

## 2. Reseller obligations (ICANN RAA 2013, Registration Data Policy 2025, transfer policy, abuse)

### Takeaway
A gTLD reseller inherits the registrar's RAA duties by contract. The main ones:
- **Verify the registrant email within 15 days** of a registration, transfer or registrant change, or the domain is suspended.
- Show the registrant rights document and the registrar's terms.
- Keep contact data accurate.
- Honor transfer rules, including the 60-day post-registration and post-transfer windows and the Change of Registrant lock.

The **Registration Data Policy** became mandatory on 21 Aug 2025. It shrinks data collection (admin, tech and billing contacts are being dropped) and makes the **Organization field define the legal owner**. Transfer policy reform (47 recommendations) is approved but not yet in force.

### Cited Findings
- On RAA 2013 verification, ICANN's own blog says: if you fail to verify within 15 days of a request, the registrar may suspend or terminate the domain. — [ICANN blog 2014 (stale but still the operative rule)](https://www.icann.org/en/blogs/details/the-2013-registrar-accreditation-agreement--what-you-need-to-know-13-2-2014-en)
- Verification is triggered by registration, transfer-in, or a change to registrant or account-holder data. If the registrant does not respond, the registrar must verify manually or suspend. — [ICANN RAA FAQ presentation 2014](https://archive.icann.org/meetings/singapore2014/en/schedule/wed-raa-faqs/presentation-raa-faqs-26mar14-en.pdf)
- A reseller implementation (ResellerClub):
  - The link must be clicked within 15 days or every domain tied to that email is suspended.
  - Each domain gets its own 15-day clock, but the email only needs verifying once.
  - Reminders go out on days 5, 10 and 13 (OpenSRS-style practice).
  — [ResellerClub](https://www.resellerclub.com/help/article/RAA-Email-Verification-and-Domain-Suspension-Rules); [OpenSRS](https://opensrs.com/blog/registrant-verification-announcement)
- eNom's reseller guide lists RAA reseller recommendations. For contact changes made through the website, an unverified change is cancelled after 7 days, so the window differs between the UI and the API path. — [eNom RAA reseller](https://cp.enom.com/help/raa-resellers.aspx); [eNom RAA FAQ](https://www.enomcentral.com/help/raa-faq.aspx)
- **Registration Data Policy (RDP):**
  - It took effect for all contracted parties on **21 Aug 2025** after a transition starting 21 Aug 2024.
  - It replaces the Interim Registration Data Policy.
  - It governs collection, transfer to the registry, publication, redaction, retention, and handling of requests to disclose non-public data.
  — [INWX](https://www.inwx.de/en/blog/icann-rdp-update); [Nicnames](https://nicenames.com/en/news/icann-registration-data-policy-enters-into-force-for-contracted-parties-on-21-august-2025); [ICANN compliance disclosure dashboard](https://compliance-reports.icann.org/disclosure/dashboard/disclosure-list.html)
- Under the RDP, registrars delete admin, tech and billing contacts and the registrant fax unless the registry requires them. **If the Organization field is filled, that organization is the Registered Name Holder (legal owner).** If it is blank, the named individual is. — [INWX](https://www.inwx.de/en/blog/icann-rdp-update); [OpenSRS RDP for resellers](https://opensrs.com/blog/what-icanns-updated-registration-data-policy-rdp-means-for-opensrs-resellers); [Ascio RDP](https://www.ascio.com/blog/what-icanns-updated-registration-data-policy-rdp-means-for-ascio-resellers/)
- **Transfer Policy Review:**
  - The GNSO Council approved all 47 recommendations on 12 Mar 2025 and asked for an Implementation Review Team.
  - Board public comment ran 28 Apr to 16 Jun 2025.
  - Under the Bylaws the Board must adopt unless a 2/3 vote rejects.
  — [GNSO motion](https://lists.icann.org/hyperkitty/list/council@icann.org/message/ODZWZLJOGLHNAYX6BHGHRW5CD2ALSSVE/attachment/2/GNSOMotiontoapprovetheTransferPolicyReviewFinalReport.docx); [TPR notice](https://lists.icann.org/hyperkitty/list/gnso-tpr@icann.org/thread/VCH2UA4SRV7KOCDBESC6Z6YJ5OCC2WEI/); [CADE](https://cadeproject.org/updates/domain-transfer-policy-reform-advances-toward-final-icanns-board-approval/)
- A secondary vendor source (NiceNIC) says the Board approved the 47 recommendations on **7 Jun 2026**, with implementation pending and the current workflow unchanged until then. The current rules it describes:
  - A registrar may deny transfer within 60 days of initial registration.
  - It may deny within 60 days of a prior inter-registrar transfer.
  - The Change of Registrant confirmation and lock apply, with an opt-out the registrar may offer in advance.
  — [NiceNIC](https://nicenic.com/vi/support/What-Is-the-60-Day-Domain-Transfer-Lock-4853) (unverified against ICANN Board resolutions)

### Inferences
- For biveit, do three things:
  1. Send the verification email in biveit's own branding, or make sure the upstream registrar's email clearly names biveit.
  2. Show a "verify your email" banner in the dashboard with a live countdown.
  3. Treat "suspended for unverified email" as a first-class domain state, so the site's breakage is explained in-product.
- Because the **Org field = owner** rule is now in force, the registrant form should not silently fill in the biveit org or a user's company name. If an employee buys for the company, ownership follows the org field. Never put "biveit" as registrant: it violates the RAA reseller duties and would make the platform the legal owner of every domain.
- Design transfer-out (auth code + unlock) so it does not hard-code the 60-day lock or the Change of Registrant lock. They are likely to change once TPR implementation lands (a TPR-proposed 30-day lock and TAC changes are **[unverified/background]**).

### Gaps
- I did not fetch the RAA text directly: the Registrant Rights and Responsibilities doc, §3.12 reseller obligations, and the abuse-contact §3.18 duties (abuse email and phone, 24/7 law-enforcement contact, 24h response to LEA reports). **[unverified/background]**
- The ICANN DNS Abuse amendments (effective 5 Apr 2024, requiring "prompt" mitigation of malware, botnets, phishing, pharming and spam-as-vector) are **[unverified/background]**. A reseller would likely be contractually bound to forward abuse reports and act on them. Verify on icann.org.
- I could not confirm the ICANN Board resolution date or the TPR implementation effective date from a primary source.
- Default WHOIS privacy: redaction under RDP already hides personal data, but whether a privacy/proxy service is still needed is not resolved here.

## 3. Lifecycle facts (AGP, grace and redemption, ERRP, .ai, HSTS TLDs, premiums, IDNs)

### Takeaway
The **5-day AGP** gives a full registry refund on deletion, but excess deletes are penalized, and some resellers sell AGP-refund as a separate feature. .ai now requires a **2-year minimum** (Identity Digital registry since Jan 2025). .dev and .app are **HSTS-preloaded at the TLD level**, so the site is unreachable until TLS works. Netlify does not sell IDNs.

### Cited Findings
- ICANN defines the AGP as 5 days after registration, during which a delete gets the registrar full credit from the registry. The AGP Limits Policy charges for excessive deletes (US$0.20 per delete above the threshold in the original provision). — [ICANN AGP status report (2009, stale but the policy remains)](https://www.icann.org/resources/pages/agp-status-report-2009-08-12-en)
- .US example cap: free AGP deletes are limited to the greater of 50 or 10% of monthly net adds. — [About.us AGP policy](https://www.about.us/documents/policies/AGP_(Add_Grace_Period)_Limits_Policy.pdf)
- CentralNic Reseller sells "Refunded AGP Deletes" as a **paid** service. The AGP is usually 5 days, but some ccTLDs differ, and privacy or trustee add-ons are not refundable. So AGP refunds are **not automatically passed through** by every reseller platform. — [CentralNic Reseller KB](https://kb.centralnicreseller.com/services/paid-services/)
- **.ai:** Identity Digital has managed the registry since Jan 2025. The minimum registration and renewal term is 2 years (max 10), and 1-year .ai is impossible. — [Dynadot (May 2026)](https://www.dynadot.com/help/question/ai-domains-year-restrictions); [NameSilo (Oct 2026)](https://www.namesilo.com/blog/en/domain-name-search/ai-domain-registry-rules-pricing-and-renewals); [Wikipedia .ai](https://en.wikipedia.org/wiki/.ai)
- **.dev/.app/.page/.day/.new** (Google Registry): the whole TLD is on the HSTS preload list. HTTP never loads, and without a valid certificate the browser refuses the site. — [Porkbun KB](https://kb.porkbun.com/article/96-hsts-preload-and-google-registry); [Wikipedia .dev](https://en.wikipedia.org/wiki/.dev)
- Netlify does not support registering IDNs. — [Netlify docs](https://docs.netlify.com/domains/configure-domains/register-and-buy-a-domain/)
- Vercel's search shows premium status inline. — [Vercel changelog](https://vercel.com/changelog/vercel-domains-at-cost-pricing-and-the-fastest-on-the-web)
- A generic (non-authoritative) guide gives roughly a 30-day grace period plus a 30-day redemption period with a restoration fee after expiry. — [domaindetails.com](https://domaindetails.com/kb/troubleshooting/domain-renewal-failed)

### Inferences
- When buying .dev or .app, the TLS certificate must be issued before or right at "active", or the user sees a hard browser error. Prefer HTTP-01 on platform nameservers, or DNS-01 since biveit controls the zone.
- Store `min_years` and `allowed_years` per TLD in the pricing catalogue (.ai = 2). Price display and checkout must use the registry minimum, not assume 1 year.
- Do not promise refunds the reseller account cannot deliver. Check whether the chosen reseller passes AGP credits through.

### Gaps
- ERRP: the ICANN page redirected to a 404. **[unverified/background]**:
  - Two reminders before expiry (about 1 month and 1 week).
  - One reminder within 5 days after expiry.
  - DNS must be interrupted ("parked") at least 8 days after expiry, with the original path to renewal preserved.
  - A 30-day Redemption Grace Period for registries that offer it.
  - Fees must be disclosed.
  Verify on icann.org (Consensus Policies, ERRP).
- Auto-renew grace (0–45 days, registry-dependent) and the exact registry RGP fees were not sourced.
- Premium pricing: renewals for premium names often keep premium price tiers. Not sourced.
- .vn/.com.vn lifecycle (VNNIC grace and renewal windows) was not found.

## 4. Payments and finance (prepaid balance, USD vs VND, VAT/e-invoice in VN, markup strategy)

### Takeaway
Vercel competes on **at-cost pricing**. Hostinger and other hosters use **cheap first year, retail renewal**. Framer and Squarespace-style bundles give a **free first year with an annual plan**. For .vn, the state fees are set by MoF circulars (Thông tư 20/2023 for registration, 10/2025 for maintenance), and registrars do not charge VAT on those fee components.

### Cited Findings
- Vercel prices "at registrar cost" with no upsells. — [Vercel changelog](https://vercel.com/changelog/vercel-domains-at-cost-pricing-and-the-fastest-on-the-web)
- Hostinger .com is $9.99 first year and $19.99 renewal, plus a $0.20 ICANN fee (aggregator data). — [domainoffer.net](https://domainoffer.net/tld/com/hostinger)
- Framer gives a free domain bundled with annual plans for year one, then standard renewal pricing. — [createwith.com](https://www.createwith.com/tool/framer/updates/framer-bundles-free-custom-domain-with-annual-site-plans); [Hover](https://support.hover.com/support/solutions/articles/201000124624-framer-and-hover-custom-domain-partnership)
- **.vn fees:**
  - The registration fee is one-time and the maintenance fee is annual.
  - Thông tư 20/2023/TT-BTC is in effect from 1 Jun 2023, with a registration fee of about 100,000 VND for the listed categories.
  - Thông tư 10/2025/TT-BTC is in effect from 3 May 2025: a 1-character 2nd-level .vn costs 40M VND/yr and a 2-character one 10M VND/yr.
  — [VnEconomy](https://vneconomy.vn/tu-3-5-phi-duy-tri-su-dung-ten-mien-quoc-gia-vn-cao-nhat-40-trieu-dong-nam.htm); [VnEconomy 2023](https://vneconomy.vn/ten-mien-vn-cap-2-mot-ky-tu-co-phi-duy-tri-len-toi-40-trieu-dong-nam.htm)
- Through 31 Dec 2025, id.vn (citizens aged 18–23) and biz.vn (new businesses) pay fees only from year 3, for the first new domain only. A draft would add on.vn, ad.vn and ap.vn and extend the waiver to 2030 (adoption not confirmed). — [Thời báo Tài chính](https://thoibaotaichinhvietnam.vn/bo-tai-chinh-de-xuat-thu-phi-3-ten-mien-vn-moi-keo-dai-uu-dai-den-nam-2030-201060.html); [Báo Pháp luật](https://doanhnhan.baophapluat.vn/giam-50-muc-thu-mot-so-le-phi-dang-ky-su-dung-ten-mien-quoc-gia-vn-51270.html)
- A registrar contract template says the registrar does not charge VAT on the .vn registration and maintenance fees. This is a contract clause, not a statute. — [Thư viện Pháp luật: hợp đồng nhà đăng ký tên miền .VN](https://thuvienphapluat.vn/hopdong/331/HOP-DONG-NHA-DANG-KY-TEN-MIEN-VN)
- **.vn registrant data** (from 25 Dec 2024, under Decree 147/2024):
  - Individuals must provide full name, date of birth, personal ID number (or passport), permanent and temporary address, phone and email.
  - Household businesses must also provide registration and tax codes.
  — [Thư viện Pháp luật Q&A](https://thuvienphapluat.vn/hoi-dap-phap-luat/thong-tin-nao-phai-cung-cap-khi-dang-ky-su-dung-ten-mien-tu-25122024-138038198.html); [form sample](https://hoatieu.vn/bieu-mau/mau-ban-khai-dang-ky-ten-mien-vn-danh-cho-ca-nhan-188705)

### Inferences
- A .vn retail price is made of three parts: state registration fee (no VAT), state maintenance fee (no VAT), and the registrar or reseller service fee (VAT-able). The invoice must split these lines.
- For gTLDs bought in USD from a foreign reseller and sold in VND, the cost is a USD import of service. Retail VND pricing needs an FX buffer and periodic repricing, and renewals should be priced at renewal time, not locked at purchase.
- The .vn flow needs a much heavier KYC form (personal ID number, DOB, address) than gTLDs. This is personal data under Vietnam's PDP decree and law, so it has storage and consent implications.

### Gaps
- No primary sources found for:
  - prepaid reseller balance mechanics (low-balance alerts, auto top-up APIs) at name.com, OpenSRS or Openprovider;
  - Vietnam VAT rate on the gTLD service fee (likely 10%, possibly reduced to 8% under the temporary VAT cut; check the current resolution);
  - e-invoice (hóa đơn điện tử) treatment of state fees;
  - whether a foreign-sourced cost triggers foreign contractor tax (thuế nhà thầu).
- Vietnamese .vn resale requires going through a VNNIC-accredited registrar (Mat Bao, PA Vietnam, Nhân Hòa, etc.), since VNNIC does not sell directly. The exact reseller contract terms were not researched.

## 5. Engineering patterns (idempotency, state machine, async, reconciliation, caching, routing)

### Takeaway
The public evidence comes from failure modes: paid but not renewed (Shopify), clientHold the user cannot clear (Vercel), and a registrar silently re-routed (Wix). Each points to the same design: a local order and domain **state machine**, **idempotent** registrar calls, **re-checking availability right before charge**, and a **reconciliation job** that treats the registrar as the source of truth for domain status and expiry.

### Cited Findings
- Vercel's architecture hints for fast availability: structured concurrency, layered caching, Bloom filters, partitioned batching, and streaming results. — [Vercel changelog](https://vercel.com/changelog/vercel-domains-at-cost-pricing-and-the-fastest-on-the-web)
- Wix routes registrations to a different registrar when the first is unavailable "due to technical reasons". That is multi-registrar failover in production. — [Wix–Tucows agreement](https://www.wix.com/domains/domain-registration-agreement/tucows)
- Billing/registrar drift seen in the wild:
  - Shopify "Domain paid but still pending renewal".
  - Shopify "Domain renewal failed" emails arriving daily although the card is valid, because the issuer blocks only the domain charge.
  — [Shopify community](https://community.shopify.com/t/domain-paid-but-still-pending-renewal/412026); [Shopify JP](https://community.shopify.com/t/topic/324013)
- On Netlify, the domain is bought and then the DNS zone and certificate follow automatically. The cert waits on propagation, an implicit async step. — [Netlify docs](https://docs.netlify.com/domains/configure-domains/register-and-buy-a-domain/)

### Inferences
These are design suggestions, not sourced facts.
- **Order state machine**: `quoted → payment_pending → paid → registering → active` with branches `→ failed → refund_pending → refunded`. The domain entity is separate (`active → expiring → expired/grace → redemption → deleted`, plus `suspended_unverified` and `client_hold`). Keep an append-only event log per domain (registrar calls and responses, payments). This follows the project's table/stream duality.
- **Idempotency**: generate a client order ID before calling the registrar, and pass it if the API supports it. On timeout, **query the domain at the registrar before retrying the create**: "already registered by us" means success. Never charge twice: capture payment once, keyed by order ID.
- **Ordering**: authorize the card, re-check availability and price (live, uncached), register, then capture. Alternatively capture first and refund automatically on failure. Rely on the AGP window only if the reseller passes AGP credits through.
- **Reconciliation**: run a nightly job listing all domains at each reseller account and diff them against the local DB (expiry date, status flags such as clientHold or serverHold, nameservers, auto-renew flag, verification status). Alert on drift. This is the only defense against "paid but not renewed".
- **Renewal**: renew biveit-side ahead of expiry (for example at T-30, retrying until T-7) rather than relying on the registrar's own auto-renew. Keep registrar-side auto-renew OFF to avoid unpaid renewals billed to the reseller balance. Alternatively keep it ON as a safety net and accept the balance risk. Pick one; this is a product decision.
- **Availability cache**: cache only "taken" results (stable) for a short TTL. Treat "available" as advisory and always re-check at checkout. Premium status and price must come from the registrar at checkout.
- **Routing**: a TLD-to-registrar-account table (for example name.com or OpenSRS for gTLDs, a VN registrar for .vn/.com.vn) behind a single registrar interface module. Repo, service and module separation applies.

### Gaps
- I did not fetch name.com, OpenSRS or Openprovider API docs on idempotency keys, webhooks vs polling, or rate limits. The registrar-choice research thread covers those.

## 6. Known failure stories

### Takeaway
The best-documented recent failure is the **July 2024 Squarespace hijack of migrated Google Domains accounts**. The cause was account takeover via weak migration and auth flows, not the registry. Other recurring patterns are suspensions for unverified registrant email, renewals failing on card issues, and reseller-locked domains the upstream registrar refuses to support.

### Cited Findings
- **Squarespace, 9–12 Jul 2024**:
  - DNS hijacks hit DeFi front-ends (Compound, Celer, Pendle, Unstoppable Domains) that had moved over from Google Domains, and the sites were redirected to wallet drainers.
  - Reported factors: MFA disabled for migrated accounts, and an attacker could sign up with email/password using an email tied to the domain, because Squarespace assumed social login.
  - The root cause is not officially confirmed.
  — [BleepingComputer](https://www.bleepingcomputer.com/news/security/dns-hijacks-target-crypto-platforms-registered-with-squarespace/); [SiliconANGLE](https://siliconangle.com/2024/07/15/multiple-crypto-domains-hijacked-squarespace-due-google-domains-migration-flaw/); [Blockaid](https://blockaid.io/blog/squarespace-defi-domain-hijack-incident); [Namefi analysis](https://namefi.io/r/en/blog/the-2024-squarespace-defi-domain-hijacks)
- **Vercel clientHold**: the domain is stuck, and the upstream (name.com) refuses because it is "partner-managed". — [Vercel community](https://community.vercel.com/t/my-domain-is-stuck-on-clienthold-name-com-confirmed-partner-managed-by-vercel/49981)
- **Shopify**: some merchants lost domains after renewals stalled even with auto-renew on, per a third-party blog (low authority). — [SureBright](https://www.surebright.com/blog/is-your-shopify-domain-renewal-stuck-or-failing-heres-what-you-can-do)
- **Wix**: a domain with a Tucows/OpenSRS registrar appeared under someone else's Wix account, and support pointed the user to Tucows. Account mapping drifted. — [asktech.support](https://www.asktech.support/software/se2um-domain-cannot-access-via-wix-account.html)
- **Google-to-Squarespace transfers**: forum reports of domains that "cannot be renewed" (anecdotal). — [dn.ca forum](https://dn.ca/topic/google-transferred-domains-to-squarespace-cannot-be-renewed.4570)

### Inferences
- Domain control equals site control. Changing nameservers, DNS or contacts, or requesting an auth code, should require re-auth or 2FA, plus email notification to the registrant. Never bulk-migrate accounts with weakened auth.
- Everything goes through the reseller (biveit). So if the biveit account is suspended, the balance runs dry, or the business shuts down, the user's domain is stranded. An exit path (auth code on request, documented in the ToS) is required.

### Gaps
- Classic reseller-collapse cases (RegisterFly 2007, after which ICANN required registrar data escrow, and the Epik troubles of 2023) were not fetched. They are stale (<2024) **[unverified/background]**.
- I did not find HN or Reddit threads specifically on "domain suspended for unverified email" for site builders, though the RAA mechanism is well documented above.
