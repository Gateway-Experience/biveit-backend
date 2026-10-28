# The launch flow (docs/plans/launch-flow.md): a user taking their approved site live -- picking a plan and a
# domain, paying, and the platform then setting it all up. Glue over three modules, which know nothing of each
# other: payments (what's bought), domains (the registrar), deployments (the site itself).
#   offers.py      domains as we sell them: search results and quotes, with our price
#   checkouts.py   starting a launch: validating the choice, recording it, the provider's checkout links
#   fulfilment.py  after a payment: registering the domain, its DNS, serving the site -- re-run until done
#   status.py      where a project's launch stands, as the FE shows it (and switching the active domain)
# Nothing here is stored by launch itself: a launch is its `launch.*` events (the choice, failures) plus what
# payments and deployments hold.
