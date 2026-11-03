# The launch flow (docs/launchflow.txt -- the flow, the domain terms and rules; docs/plans/launch-flow.md for pricing
# and the UI): a user taking their approved site live -- picking a plan and a domain, paying, and the platform then
# setting it all up. Glue over three modules, which know nothing of each other: payments (what's bought), domains
# (the registrar), deployments (the site itself).
#   access.py         what's been bought, as the flow sees it (payments' access, minus a domain that failed)
#   offers.py         domains as we sell them: search results and quotes, with our price
#   checkouts.py      starting a purchase: validating the choice, the provider's checkout link; upgrading a plan
#   fulfilment.py     after a payment: registering a bought domain, giving the site an active domain -- re-run
#                     until done
#   domain_events.py  what happened to a domain we bought (domain.registered / domain.failed), as events
#   status.py         where a project stands, as the FE shows it (and adding the user's own domain, switching)
#   subdomains.py     the free option: a subdomain of ours (<slug>.<PROD_DOMAIN>), and suggesting its name
# "Launch" is only the flow's name, not a thing we store: what's bought is payments' (subscriptions, and the
# checkout events that started them), the domains and the site are deployments'.
