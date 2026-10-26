# Reselling .vn / .com.vn domains programmatically from a Vietnamese SaaS (as of Oct 2026)

## Legal framework: what governs .vn now, and what it says about resellers, registrars and registrant identity

### Takeaway
Decree 72/2013 has been replaced. Three documents now govern .vn. **Decree 147/2024/ND-CP** sets the rules. **Circular 48/2025/TT-BKHCN** (Ministry of Science and Technology, effective 10/02/2026) sets the procedures; it replaced Circulars 24/2015 and 21/2021 of the old MIC. **Circular 10/2025/TT-BTC** sets the fees. The law lets a registrar build a "distribution channel" (kênh phân phối, which is what an "đại lý" is) on a contract. Selling .vn without being a registrar or holding a contract with one is fined. Foreign registrars may only serve registrants located abroad, so a Vietnamese user's .vn has to go through one of the 10 domestic registrars, either directly or through a reseller contract. The registrant must be the real end user, and identity has to be verified by digital signature, by the national e-ID system, or by legally valid documents.

### Cited Findings
- Decree 147/2024/ND-CP is dated 09/11/2024 and replaces Decree 72/2013/ND-CP. VNNIC's registrar-policy page cites Decree 147/2024, Decree 15/2020 (penalties), Circular 48/2025/TT-BKHCN and Circular 10/2025/TT-BTC as the current legal basis — [VNNIC – Chính sách đối với nhà đăng ký](https://vnnic.vn/vi/ten-mien-vn/danh-cho-nha-dang-ky/chinh-sach-doi-voi-nha-dang-ky)
- Domain-related provisions of Decree 147 have been in force since 25/12/2024, including 6 revocation cases — [Tạp chí Công Thương](https://tapchicongthuong.vn/6-truong-hop-se-bi-thu-hoi-ten-mien---vn--tu-ngay-25-12-2024-129681.htm)
- Decree 147 Art. 9.4: registration is non-discriminatory and first-come-first-served, except for auctioned, protected and shared names — [Decree 147 Ch.2 Sec.2 (hethongphapluat)](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Decree 147 Art. 9.7, information the registrant must provide —
  - Organizations: name, electronic identifier, enterprise code, tax code, head-office address, phone, email, the domain manager and technical manager, the fee payer, and the purpose if the domain is for a news site, social network or aggregator.
  - Individuals and household businesses: full name, date of birth, "số định danh cá nhân hoặc số CMND hoặc số hộ chiếu", place and date of issue, tax code, permanent and temporary address, phone, email.

  [Decree 147 Ch.2 Sec.2](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Decree 147 Art. 9.8: the person filing is verified "thông qua chữ ký số", through the electronic identification and authentication system, or an equivalent technology. Art. 12.3a: service providers must receive, verify and store this information accurately — [same](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Decree 147 Art. 9.13–9.16, suspension and revocation —
  - Suspension: the domain is suspended if its information is inaccurate, the holder cannot be identified, or the file is incomplete. The holder has **30 days** to fix it, and revocation follows if they don't.
  - Unpaid maintenance fee: suspended, then revoked if the fee is still unpaid 30 days after the due date.
  - While suspended: no change of holder, no DNS change, no change of registrar, no transfer.

  [same](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Decree 147 Art. 9.10: a holder may not grant subdomains of its own domain to other parties. This is relevant if biveit wanted to hand out `user.biveit.vn`, though that is a different product from reselling — [same](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Decree 147 Art. 13, who can be a registrar —
  - A domestic registrar must be a Vietnamese organization or enterprise. It can serve registrants both in Vietnam and abroad.
  - A foreign registrar (with an ICANN contract) **may serve only registrants located abroad**.
  - Both need suitable technical capacity and a contract with VNNIC. VNNIC answers an application within 30 days.

  [same](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Decree 147 Art. 12.4: domestic registrars must run DNS on at least 2 server clusters in Vietnam, may charge service fees by agreement, and "trong trường hợp phát triển kênh phân phối, phải có hợp đồng với tổ chức, doanh nghiệp tham gia kênh phân phối". A channel member does not act as the registrar toward VNNIC. The decree says "kênh phân phối", not "đại lý" — [same](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2); [VNNIC policy page](https://vnnic.vn/vi/ten-mien-vn/danh-cho-nha-dang-ky/chinh-sach-doi-voi-nha-dang-ky)
- Decree 15/2020 Art. 45.4(d): providing .vn registration or maintenance services without being a .vn registrar, or without a domestic agency contract, is fined **70–100M VND** (as cited by VNNIC) — [VNNIC policy page](https://vnnic.vn/vi/ten-mien-vn/danh-cho-nha-dang-ky/chinh-sach-doi-voi-nha-dang-ky)
- Decree 147 Art. 9 and Art. 19.2 also cover international domains (.com etc.) used by Vietnamese holders: the holder must notify the Ministry. Domestic sellers of international domains must report quarterly via thongbaotenmien.vn and run 2 DNS clusters in Vietnam — [Decree 147 Ch.2 Sec.2](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Circular 48/2025/TT-BKHCN was issued 25/12/2025 by the Ministry of Science and Technology (VNNIC now sits under it) and takes effect 10/02/2026 — [VNNIC announcement](https://vnnic.vn/tenmien/chinhsach/bo-khoa-hoc-va-cong-nghe-ban-hanh-thong-tu-so-48-2025-tt-bkhcn-ngay-25-12-2025); [MOST](https://mst.gov.vn/van-ban-phap-luat/25274.htm)
- Circular 48/2025 replaces 24/2015/TT-BTTTT and 21/2021/TT-BTTTT (Art. 33). The source is a third-party reproduction of the text, which was truncated — [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Circular 48/2025, the registration declaration (bản khai): it is submitted as Mẫu 01a (organization), 01b (household business) or 01c (individual). Minors under 18 need a parent's confirmation (Mẫu 02); children under 6 must register through a legal representative — [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Circular 48/2025, how to file: online with a digital signature, or in person / by post if that isn't possible. .vn applications go to registrars listed at nhadangky.vn; international domains go via thongbaotenmien.vn — [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Circular 48/2025, identity verification (Art. 9), one of three methods —
  1. Match the digital-signature certificate against the registration data.
  2. Connect to the national electronic identification and authentication system (định danh và xác thực điện tử, i.e. the VNeID / C06 infrastructure) and match the data.
  3. Submit legally valid documents in person or by post.

  Data that has been verified once can be reused for later registrations — [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Circular 48/2025, deadlines: the registrar must notify the result within **3 working days** of a valid application. For missing information, there is a 15-day window to update, then 30 days of suspension, then revocation; updating during the suspension restores the domain — [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- The September 2025 draft of the circular said registration information must be checkable against national databases, and that the registrar or VNNIC may request proof documents when it can't be checked — [LSVN, 03/09/2025](https://lsvn.vn/du-kien-quy-dinh-moi-ve-trinh-tu-dang-ky-ten-mien-a162536.html)
- VNNIC's .vn page promotes eKYC as a way to verify identity "hoàn toàn trực tuyến" from a phone or computer, but gives no specification — [VNNIC – Tên miền .vn](https://www.vnnic.vn/vi/ten-mien-vn)
- In practice registrars collect identity documents themselves. TotHost (a reseller) has required eKYC for all individuals since 01/07/2024, with photos of both sides of the CCCD, while organizations upload a signed and stamped (or e-signed) bản khai. It cites VNNIC requirements. The post is from 2024 and predates Circular 48 — [TotHost (18/07/2024)](https://tothost.vn/en/totnews-promotions/ekyc-instructions-when-registering-domains)
- iNET's reseller policy —
  - Resellers must submit a signed and stamped bản khai for .vn, plus a copy of the CCCD for the registrant or organization representative.
  - A domain cannot go live until the registrant profile is complete.
  - Resellers "must not register domains for customers using their own information".
  - Resellers must guide customers to declare international domains at thongbaotenmien.vn.

  [iNET – Chính sách đại lý](https://inet.vn/dai-ly/chinh-sach)
- Older context: VNNIC's 2015 era also had a template "Hợp đồng nhà đăng ký tên miền .vn" — [ThuVienPhapLuat contract template](https://thuvienphapluat.vn/hopdong/331/HOP-DONG-NHA-DANG-KY-TEN-MIEN-VN)

### Inferences
- **Becoming a registrar is out of scope for biveit.** It requires a VNNIC contract, 2 in-country DNS clusters, EPP integration and VNNIC review. The realistic model is a contracted distribution-channel partner (đại lý) of one domestic registrar. That contract is also what avoids the 70–100M VND fine.
- **Registering in biveit's own name for users is a bad idea and looks non-compliant.**
  - The decree makes the registrant responsible for accurate identity, and iNET's policy explicitly forbids it.
  - Legally the user would not own their domain. Transfer later would be a "change of holder" needing a written instrument stating the domain's value (Circular 48).
  - Every domain would attach biveit to content liability, and biveit would face revocation risk for inaccurate information.
- **Submitting ID via API on the user's behalf.** The law does not forbid a channel partner collecting and forwarding the bản khai and ID data; registrars clearly expect resellers to do exactly that. The verification step itself, though, has to be a digital signature, an e-ID match, or a document. So the product needs a KYC step per registrant:
  - an upload of CCCD front and back, or the registrar's eKYC link; or
  - an e-signed bản khai, typically for organizations.

  Expect the registrar API to accept contact fields, while verification may be a separate upload or an asynchronous flow. A domain will likely sit in a pending or unactivated state until verification completes, with roughly a 3-working-day SLA.
- **Unverified domains** follow Decree 147: suspension, then revocation after 30 days. The product should surface "verification pending" and "suspended" states and chase the user.
- The 2026 push to tie Internet resources to national e-ID (Directive 57, Decree 320/2026 on e-ID) suggests VNeID-based verification will tighten further. These come from secondary sources only — [search summary of local gov news](https://ubmttqvn.quangtri.gov.vn/index.php/to-chuc-tuyen-giao/nhung-thay-doi-lien-quan-den-vneid-tu-ngay-28-9-2026-8549.html)

### Gaps
- I could not read the official full text of Circular 48/2025. The thuvienphapluat English version returned 403, and the LuatVietnam copy was truncated, so the Mẫu 01a–04 forms are missing. Whether it details distribution-channel obligations or API-submitted verification is unconfirmed.
- No primary source says whether the VNeID route is exposed to registrars via an API that a reseller can trigger, such as a deep link to the VNeID app.
- Decree 15/2020 has been amended since (e.g. Decree 14/2022). I did not verify that the 70–100M VND fine article is unchanged; VNNIC's page still cites it.
- No historical figure was found for the deposit a registrar pays VNNIC (the brief's "50M VND"). It is irrelevant if biveit is a reseller.

## .vn specifics: second-level options, fees, retail prices, terms, transfers, nameservers

### Takeaway
.vn pricing is mostly state fees plus a thin registrar margin. Under Circular 10/2025/TT-BTC (effective 03/05/2025):

| Name | Registration fee (one-off) | Maintenance fee (per year) |
|---|---|---|
| .vn | 100k VND | 350k VND |
| .com.vn / .net.vn / .biz.vn / .ai.vn | 100k VND | 250k VND |
| .id.vn | 10k VND | 50k VND |
| .io.vn | 10k VND | 20k VND |

Terms are 1–10 years. A domain cannot change registrar within 60 days of registration or within 30 days before expiry. Circular 48/2025 added new SLDs: .ai.vn, .io.vn, .ap.vn, .ad.vn, .on.vn, .health.vn.

### Cited Findings
- Fee schedule (Circular 10/2025/TT-BTC; issued 19/03/2025, effective 03/05/2025, replaces 23/2023/TT-BTC) —

  | Category | Registration fee | Maintenance fee / year |
  |---|---|---|
  | 1-char .vn | 100k | 40M |
  | 2-char .vn | 100k | 10M |
  | Other .vn | 100k | 350k |
  | com.vn, net.vn, biz.vn, ai.vn | 100k | 250k |
  | edu, gov, org, ac, health, int, provincial | 50k | 100k |
  | info.vn, pro.vn | 10k | 50k |
  | id.vn | 10k | 50k |
  | io.vn | 10k | 20k |
  | name.vn | 10k | 20k |
  | Vietnamese-script (IDN) | 10k | 20k |

  Promotion valid 03/05/2025–31/12/2025: id.vn for citizens aged 18–23, and biz.vn for enterprises under 12 months old, pay maintenance only from year 3.

  [Luật Việt An summary](https://luatvietan.vn/quy-dinh-ve-phi-le-phi-su-dung-ten-mien-theo-thong-tu-10-2025-tt-btc.html); partly confirmed by [VNNIC policy page](https://vnnic.vn/vi/ten-mien-vn/danh-cho-nha-dang-ky/chinh-sach-doi-voi-nha-dang-ky) (100k registration, 350k other 2LD, 250k com.vn/net.vn/biz.vn/ai.vn)
- Decree 147 Art. 9.9 gives Vietnamese citizens aged 18–23 priority and fee exemption or reduction for .id.vn, and gives registered businesses priority for .biz.vn — [Decree 147 Ch.2 Sec.2](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- Circular 48/2025, second-level structure —
  - Open to everyone: COM, BIZ, NET, PRO, INFO, HEALTH, AC, IO, AI, AP (software/apps), AD, ON .VN.
  - Organizations only: ORG, INT.
  - Education organizations only: EDU.
  - State bodies only: GOV.
  - Personal, trade or alias names: NAME.
  - **Vietnamese citizens only**: ID.VN.
  - Also provincial names, dedicated 2LDs and 3LDs.
  - Labels: a–z, 0–9, Vietnamese characters and hyphens, at most 63 characters per level.

  [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Since 10/02/2026 only education-sector organizations can register or renew .edu.vn — [WebNIC KB .vn](https://faq.webnic.cc/kb/vn/)
- Transfers under Circular 48/2025 —
  - Change of registrar needs the agreement of both registrars at the holder's request. It is barred within 60 days of first registration, within 30 days before expiry, and during suspension, violation handling or a dispute.
  - Change of holder follows Art. 55 of Decree 115/2025 and property law; a gift or capital contribution needs a written instrument stating the domain's value.

  [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Registration and renewal terms are 1–10 years. Partners cannot delete a .vn domain after registration — [WebNIC KB .vn](https://faq.webnic.cc/kb/vn/); same 1–10 years at [CentralNic Reseller KB](https://kb.centralnicreseller.com/domains/tlds/vn)
- Nameservers —
  - CentralNic reports 1–3 nameservers, no preconfigured DNS, and a **fee for changing nameservers** (it needs X-ACCEPT-NSCHANGE=1).
  - Circular 48 text found does not mention any requirement that nameservers be live at registration.

  [CentralNic Reseller KB](https://kb.centralnicreseller.com/domains/tlds/vn); [LuatVietnam](https://luatvietnam.vn/thong-tin/thong-tu-48-2025-tt-bkhcn-huong-dan-quan-ly-va-su-dung-tai-nguyen-internet-hieu-qua-422366-d1.html)
- Retail at P.A Vietnam (Oct 2026 page) —

  | Name | Registration, first year | Renewal | List price |
  |---|---|---|---|
  | .vn | 450,000 VND | 512,000 VND (incl. 12k VAT) | 700k |
  | .com.vn | 350,000 VND | 412,000 VND | 600k |
  | .id.vn | 60,000 VND | 71,600 VND | — |
  | .io.vn | 30,000 VND | 41,600 VND | — |
  | .com | 25k (only on a 3+ year purchase) | 429k | — |

  The page is inconsistent about VAT — [P.A Vietnam bảng giá](https://www.pavietnam.vn/vn/ten-mien-bang-gia.html)
- iNET's reseller policy says resellers must sell .vn at the state-set fee, and that no VAT applies to .vn registration and maintenance fees (the state portion) — [iNET – Chính sách đại lý](https://inet.vn/dai-ly/chinh-sach)
- Premium short .vn names are periodically auctioned by VNNIC; 76 names were in the second 2026 auction — [VietnamPlus](https://www.vietnamplus.vn/76-ten-mien-dep-vn-se-chinh-thuc-len-san-dau-gia-dot-2-nam-2026-post1116629.vnp)

### Inferences
- **Margins are thin.**
  - First-year retail at PA (450k for .vn, 350k for .com.vn) equals the state fees (100k + 350k, 100k + 250k). Registrars are selling year one at cost.
  - At renewal the margin is about 150k VND on .vn and .com.vn, plus VAT on the service part.
  - A reseller discount (iNET advertises "up to 40%") probably applies only to the registrar service fee, not to the state fees.
  - So .vn is a convenience or lock-in feature for biveit, not a profit centre.
- .id.vn (about 60k per year, citizens only) and .io.vn (about 30k per year) are cheap entry options that suit a site-builder's individual users. They still need full identity verification.
- The nameserver-change fee seen at CentralNic suggests biveit should set its own nameservers at registration time.
- **Unconfirmed:** whether a registry nameserver-change fee also applies through domestic registrars.

### Gaps
- No VNNIC source confirms whether nameservers must resolve, or be preconfigured, at registration.
- I did not fetch iNET's, Nhân Hòa's or Mắt Bão's retail tables. Only PA's.
- There is no official confirmation that the id.vn and biz.vn promotion was extended beyond 31/12/2025.

## Registrar options: domestic registrars and their reseller programs / APIs

### Takeaway
VNNIC's current list has 10 domestic registrars: PA Việt Nam, Mắt Bão, Tenten (GMO-Z.com Runsystem), Nhân Hòa, iNET, ESC, Vinahost, TINO Group, BKNS, Long Vân. Viettel IDC, VNPT, FPT, AZDIGI and Hostinger are **not** on the list, so they are resellers of someone else. Two registrars have usable public evidence of a reseller API:

- **iNET** publishes a self-serve reseller program with an API and a WHMCS module. The minimum first deposit is 5M VND, the tiers run from under 20M to over 100M, and discounts go up to 40%.
- **P.A Vietnam** has a reseller API at `daily.pavietnam.vn/interface.php`. It uses username + apikey + action parameters, requires an IP whitelist, and its docs sit behind a login at kb.pavietnam.vn.

No public API docs were found for any other registrar.

### Cited Findings
- VNNIC's domestic registrars (10): PA Việt Nam; Mắt Bão; tenten (Công ty CP GMO-Z.com Runsystem); Nhân Hòa; iNET; ESC (Công ty TNHH Giải pháp Trực tuyến); Vinahost; TINO Group; BKNS (Bách Kim); Long Vân — [VNNIC – Danh sách nhà đăng ký](https://www.vnnic.vn/vi/ten-mien-vn/danh-sach-nha-dang-ky-ten-mien)
- VNNIC's foreign registrars (6): Webnic (Qinetics), Hi-Tek, Instra, EuroDNS, InternetX, hosting.kr — [same](https://www.vnnic.vn/vi/ten-mien-vn/danh-sach-nha-dang-ky-ten-mien)
- iNET reseller program (đại lý) —
  - Two tiers of partnership: Cộng tác viên (referral) and Đại lý (white-label, "chiết khấu lên tới 40%").
  - Deposit tiers: Bronze under 20M, Silver 20–50M, Gold 50–100M, Platinum over 100M VND.
  - Integration: "kết nối qua API", plus a WHMCS module for resellers on WHMCS.
  - Joining requires an organization or business with a tax code.
  - The price table did not render, and no sandbox is mentioned.

  [iNET – Đại lý](https://inet.vn/dai-ly)
- iNET reseller policy —
  - Money: the first deposit is at least 5M VND. The discount is applied at order time and excludes backorder and the domain marketplace.
  - Rejected .vn registrations: the fee is refunded if someone else registered the name first.
  - What the reseller gets: an account, API access, guides and tools.
  - Termination: on breach, the reseller must notify holders 30 days ahead, and unmigrated domains revert to iNET.
  - Reseller disappearance: iNET may renew directly with customers if the reseller is unreachable, or take over the domains if the reseller company is dissolved.

  [iNET – Chính sách đại lý](https://inet.vn/dai-ly/chinh-sach)
- iNET Software Co., Ltd is ICANN-accredited (IANA ID 3234 per a secondary source) and is a cPanel partner — [btw.media/search summary](https://btw.media/en/the-cheapest-click-in-the-stack-inet-software-and-the-cost-of-owning-the-door); [cPanel partner listing](https://partnernoc.cpanel.net/res/8819/inet-software-co-ltd.htm)
- P.A Vietnam reseller API, as seen in an unofficial open-source storefront that integrates it —
  - Endpoint: `PAVIETNAM_ENDPOINT=https://daily.pavietnam.vn/interface.php`.
  - Auth: query or form parameters `username`, `apikey` and `action`, over GET or POST form-urlencoded. The server IP must be whitelisted by PA, and the API key changes when the reseller password changes.
  - Docs: the official docs live at `kb.pavietnam.vn` and need a login.
  - Action names the author *guessed* and made configurable: checkdomain, regdomain, renewdomain, transferdomain, getcontact/updatecontact, geteppcode, lockdomain, whois, getdns/updatedns, changens, listdomain, domaininfo, balance.
  - The author's parser accepts JSON, XML or key=value responses, because they could not read the docs.
  - **Low trust for exact names.** This looks like a third-party (possibly AI-assisted) project written without access to the docs.

  [GitHub mientaymedia/api-pavietnam – docs/API-PAVIETNAM.md](https://github.com/mientaymedia/api-pavietnam); [.env.example](https://github.com/mientaymedia/api-pavietnam/blob/HEAD/.env.example)
- A "pavietnam domain registrar for whmcs" repo exists (2022) but holds only a README — [GitHub haoquangviet/whmcs-PA_vietnam](https://github.com/haoquangviet/whmcs-PA_vietnam)
- P.A Viet Nam Company Limited is ICANN-accredited, IANA ID 1649 (third-party listing) — [domaindetails.com](https://domaindetails.com/registrars/p-a-viet-nam-company-limited)
- Nhân Hòa Software Company Ltd is ICANN-accredited, IANA ID 1710 (third-party listing) — [domaindetails.com](https://domaindetails.com/registrars/nhan-hoa-software-company-ltd)
- Mắt Bão was reported around 2012 as Vietnam's first ICANN-accredited registrar. Its current ID was not found — [QĐND](https://en.qdnd.vn/politics/news/nation-gets-first-global-domain-name-registrar-431387)
- Tenten is GMO Runsystem, part of Japan's GMO Internet group, with a domain portal at domain.tenten.vn. No public reseller or API information was found — [VnExpress](https://vnexpress.net/gmo-runsystem-ra-mat-dich-vu-ten-mien-2719534.html)
- TINO was accredited as a .vn registrar more recently (Vietnamnet article, undated) — [Vietnamnet](https://vietnamnet.vn/them-nha-dang-ky-tino-gia-nhap-thi-truong-cung-cap-ten-mien-vn-2162944.html)
- GitHub repo searches for "inet.vn api", "matbao api", "nhanhoa api", "tenten.vn", "vnnic epp" and "whmcs vietnam registrar" returned nothing relevant. Searched via `gh search repos`, 2026-10-23; no URL.

### Inferences
- **iNET and PA Vietnam are the two front-runners.** Both are VNNIC .vn registrars and ICANN-accredited, so one contract would also cover .com and other gTLDs. Both advertise reseller API access.
  - iNET has the most transparent self-serve onboarding: a public form and a 5M VND minimum.
  - PA has evidence of a simple HTTP key/action API, and it is probably the largest .vn registrar.
- **Expect legacy-style APIs and plan for an adapter layer.** The evidence points to a PHP `interface.php?action=` style for PA, and REST/JSON for iNET is unverified. Docs are Vietnamese-only and behind a login. Plan to obtain the docs, sandbox details and price sheets through sales contact; I didn't do that because the brief forbids creating accounts.
- **Nhân Hòa, Mắt Bão, Tenten, BKNS, Long Vân, Vinahost, TINO and ESC** all sell .vn, and most run reseller or agent programs commercially. No public API evidence was found for any of them, so they are fallback options to query.
- Viettel IDC, VNPT, FPT and similar are just upstream resellers. Going through them adds a layer without adding legal standing.

### Gaps
- I found no public API reference for any Vietnamese registrar (endpoints, auth, sandbox, rate limits, how KYC uploads go through the API).
- iNET's per-tier price table and discount on .vn did not render.
- Deposit and discount terms for Nhân Hòa, Mắt Bão and Tenten were not found.
- Reputation data (uptime, support, fights over transfers away) was not researched; there were no reliable sources in the time budget.

## Foreign registrars/resellers offering .vn via API

### Takeaway
They are not viable for biveit's Vietnamese users. Decree 147 Art. 13 limits foreign registrars, including VNNIC-accredited WebNIC, EuroDNS, InternetX, Instra, Hi-Tek and hosting.kr, to registrants located abroad. WebNIC states outright that Vietnamese registrants must use a local registrar. CentralNic Reseller (RRPproxy) stopped new .vn registrations and transfers-in on 18 June 2026.

### Cited Findings
- Foreign registrars "chỉ được cung cấp dịch vụ cho chủ thể ở nước ngoài" (Art. 13.1b) — [Decree 147 Ch.2 Sec.2](https://hethongphapluat.com/nghi-dinh-147-2024-nd-cp-quan-ly-cung-cap-su-dung-dich-vu-internet-va-thong-tin-tren-mang/chuong-2/muc-2)
- WebNIC —
  - Registrants: .vn is open to foreign registrants without local presence, but "Vietnamese registrants must register through a local registrar".
  - Documents: a colour scan of a wet-signed form plus a passport or business certificate; registry verification is required.
  - Terms: 1–10 years, and a .vn cannot be deleted after registration.
  - SLDs supported: .vn, com, net, biz, org, info, name, edu, ac, pro, ai, id, io, int, health.

  [WebNIC KB .vn](https://faq.webnic.cc/kb/vn/)
- CentralNic Reseller —
  - Status: .vn registrations and inbound transfers ended 18/06/2026; existing domains can still be renewed or modified.
  - API fields it used: X-VN-OWNER-BIRTHDAY and X-VN-OWNER-IDENTIFICATION (ID or passport for individuals, VAT number for companies).
  - Constraints: company registrants must use the legal representative's real name, nameserver changes cost a fee, and TransferDomain is not available.

  [CentralNic Reseller KB – .vn](https://kb.centralnicreseller.com/domains/tlds/vn)
- Openprovider has a .vn KB page. Per a search snippet, if no Vietnamese address is used, proof of a business relationship with Vietnam must be sent to support. The page itself returned 403 — [Openprovider KB .vn](https://support.openprovider.eu/hc/en-us/articles/360000757968--vn)
- NiceNIC markets .vn registration and in 2026 was "recruiting domain reseller partners in Vietnam". It has a Reseller API v2 (check/register/renew/transfer/DNS, IP whitelist, reseller tier) — [NiceNIC .vn](https://nicenic.com/domain/vn-domain-registration.php); [NiceNIC news](https://nicenic.com/news/Now-Recruiting-Domain-Reseller-Partners-in-Vietnam-41129); [NiceNIC Reseller API v2](https://nicenic.com/vi/support/NiceNIC-Reseller-API-v2-4763)

### Inferences
- **The API shape is still useful as a reference.** Foreign reseller APIs (CentralNic's X-VN-* extension fields) show the minimum data the registry needs: date of birth plus an ID or tax number for the owner. That is a good guide for biveit's registrant data model.
- **Legal angle on NiceNIC.** NiceNIC is not on VNNIC's registrar list, so any .vn it sells goes through an upstream registrar. Selling .vn to Vietnamese users through it would likely breach Art. 13 / the channel rules.
- **Practical recommendation.** Use a domestic registrar (iNET or PA) for .vn and .com.vn. The same domestic ICANN-accredited registrar can also supply gTLDs, which avoids a second vendor. A foreign API (Openprovider etc.) only makes sense for gTLDs if the domestic API proves unusable, and then the thongbaotenmien.vn declaration duty for international domains still applies.

### Gaps
- Openprovider's, 101domain's, Gandi's, Dynadot's, Namecheap's and ResellerClub's current .vn support and prices were not verified; time budget, and Openprovider returned 403.
- Whether any foreign reseller still accepts Vietnam-resident registrants for .vn in practice is unknown. If one does, it appears contrary to Decree 147.
