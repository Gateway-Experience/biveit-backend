# Launch flow (business design)

Status: business flow agreed (2026-10-28); implementation not planned yet. Prices and limits are placeholders to
fine-tune. Payments go through Lemon Squeezy (merchant of record), mocked until the integration exists.

## Pricing

| | Price | Gets |
|---|---|---|
| Monthly plan | $19/mo | hosting on our platform, AI edits on the site (fair use), code download |
| Yearly plan | $159/yr (30% off) | the same + a free first year on a cheap domain bought with it |
| Just the code | $19 once | code download forever, AI edits for 30 days from payment, no hosting |
| Domain | $10-$40/yr, one yearly subscription per domain | registration + renewal through our registrar |

- One plan subscription PER SITE (project). Two sites = two subscriptions, intervals can differ.
- We absorb Lemon Squeezy's fees and taxes: prices are tax-inclusive (what's shown is what's paid).
- Fair use: the agent only works on the user's own site; a monthly allowance (number TBD) stops abuse. Users are
  responsible for their site's content and for it working; we provide the agent within those limits.
- Later: credits/discounts for users who spend a lot (derivable from the payment history, nothing to track now).

## Access (derived from payments, never stored)

- Download the code: the user has EVER paid for the project (one-time, any plan period, even if cancelled since).
- AI edits: plan active, or within 30 days of a one-time payment. Otherwise the project is read-only (+ download).
- Hosting: plan active (or within the grace period after it ends).
- Domains are only sold with an active plan (we only sell domains for sites we host).

## Domains

- Sold in fixed price points -- users only ever see a domain's own price, never a tier list:

  | Price | For registrar yearly price | Free first year with yearly |
  |---|---|---|
  | $10/yr | up to $10 | yes |
  | $15/yr | $10-15 (.com, .net, .org) | yes |
  | $20/yr | $15-20 | yes |
  | $30/yr | $20-30 | no |
  | $40/yr | $30-40 | no |
  | not sold | over $40, premium names, or a minimum term over 1 year (.ai...) | -- |

  "Registrar yearly price" = the higher of the 1-year registration and the renewal price.
- A domain is a yearly subscription (no term choice), renewed while it's paid. The user is always the registrant.
- At most ONE domain bought through us per project at a time; any number of the user's own domains.
- A site is served on exactly ONE active domain. Switching A -> B: B goes live, A stops being served (its nginx
  block and certificate go; if we bought A, its A record is removed too; a brought A's DNS is the user's -- we just
  stop answering for it).
- A domain belongs to at most one project, platform-wide.
- Bring your own: the user adds two records -- `A @ -> our IP` (traffic + certificate) and
  `TXT _biveit-verify -> <token>` (ownership proof, so nobody can attach someone else's domain that points at us).
  Checked live; not required before paying, required before going live.
- Free first year: only when the yearly plan and the domain are bought in the SAME launch. A domain bought later
  (separately) pays full price. A replacement for a domain that failed to register keeps the free year.
- No free `<slug>.biveitapp.com` address for now -- a hosted site always needs a custom domain. (To reconsider soon
  after launch: it would cover "paid but no domain yet".)

## The flow

### 1. Entry
- Approving the build shows the Launch button in the project header, permanently (even if the user goes back to
  planning). Right after approving, an optional dismissible banner points at it (not a chat message).
- The user can keep iterating; launching always ships the workspace as it is at that moment.
- Launch opens the launch dialog (never paid / one-time buyer) or the Launch tab (subscribed).

### 2. Plan
- Three cards: Monthly $19/mo, Yearly $159/yr (highlighted: "$13.25/mo, save 30%", "free domain first year"),
  Just the code $19 once. Footnote: fair use + responsibility.
- Just the code -> review. A plan -> domain step.
- One-time buyers coming back see only the two plans ("You own the code. Put it online and keep editing.").

### 3. Domain (plans only; required for now)
- Buy a domain (default tab):
  - empty search box: one-off AI suggestions (from the project's name/spec; only available, sellable names)
  - typing: search results instead; names we don't sell show "buy it at a registrar, then use Use my own"
  - each name shows its price; with yearly + eligible: ~~$15~~ **$0** first year (info: discounted by the yearly
    plan, renews at $15/yr); with monthly + eligible: "free first year with yearly" + a one-click switch
  - "Let the AI agent help you choose": closes the dialog, focuses the chat with a prefilled message. The agent can
    check names (read-only, through our backend) and recommends; the user comes back and searches the name.
- Use my own: enter the domain -> blocked if invalid, ours, or used by another project. Otherwise the two records
  with live status. Not ready -> a warning, but the user can continue ("goes live once the records are found").

### 4. Review & checkout
- Every line with today's price, what's due today, what renews when and at what price, "cancel anytime", the two
  DNS records again for a brought domain, a Terms checkbox (fair use + responsibility).
- On "Continue to payment" everything is re-checked: still available? same price? -> otherwise back to the domain
  step ("someone just took it") or "price changed, confirm".
- Payment: the plan checkout, then (if buying a domain) a second checkout for it -- "Step 2 of 2: your domain
  ($0 today)". Lemon Squeezy has no cart.
- Domain checkout abandoned: plan is active; the Launch tab says "finish your domain to go live" (resumable).

### 5. After payment
- "Payment received" -> "going live" checklist: payment confirmed, domain registered, address set up, building
  and starting the site, live. A brought domain without its records waits at "waiting for your DNS" (the site is
  already built, so it goes live the moment DNS shows up).
- The dialog can be closed anytime; the Launch tab shows the same progress.
- Done: "Your site is live at https://..." + open / preview.
- Just the code: "Thanks! Download your code."

### 6. The Launch tab (after launch)
Plan + next renewal, domain(s) + DNS status, live status + address, redeploy (5-minute cooldown), deployment
history, switch domain, add a domain, change plan, cancel.

## After launch

- Redeploy anytime with the latest changes while the plan is active (5-minute cooldown).
- Plan changes: one-time -> monthly/yearly starts right away (no proration); monthly -> yearly and yearly ->
  monthly take effect when the current term ends.
- Cancelling the plan: hosting and AI edits run to the end of the paid period, then a grace period, then the site
  is taken down. Code download stays.
- Plan renewal payment fails: the provider retries (dunning); when it gives up, same as cancelling.
- Domains: add your own anytime; buy one (if none bought yet, full price); switch the active one anytime.
- Domain renewal: charged yearly; renewed at the registrar once paid. Domain subscriptions renew ~30 days before the
  domain expires, so a failed payment has time to be retried before it lapses.
- Cancelling a domain (or its renewal failing): it stays registered until it expires, then lapses. If it's the
  active domain, the site needs another one before then.
- The domain is the user's: they can transfer it out (auth code) anytime.

## Failures

- Domain taken between review and payment: paid domain -> that payment refunded automatically; free-first-year
  domain -> nothing was charged, subscription cancelled. Only the domain part -- the plan stays. The Launch tab asks
  for another domain (free year kept).
- Deployment fails (after retries + rollback): shown in the Launch tab; we're alerted. Nothing refunded
  automatically.

## Covered elsewhere / not decided yet

- Refund policy for the plan itself (and EU withdrawal rights) -- Lemon Squeezy handles requests as merchant of
  record; our policy is TBD.
- Grace period length after a plan ends (e.g. 7 days) and what visitors see meanwhile.
- The fair-use allowance (what's counted, how much per month).
- ICANN email verification: a new domain's registrant must confirm their email within 15 days or the domain is
  suspended -- needs a visible reminder in the Launch tab.
- What happens to plan + domain subscriptions when a project is trashed / deleted (block, or cancel them?).
- Chargebacks: treat like a cancelled plan.
- Price changes: existing subscribers keep their price.
- Monthly <-> yearly switching mechanics in Lemon Squeezy (when the new price applies) -- to check in its sandbox.
- An email when the site goes live (mostly for brought domains waiting on DNS).
