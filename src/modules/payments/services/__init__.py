# The payments module's services:
#   catalog.py            what we sell (plans, domain prices, provider products) -- pure, off the constants
#   checkouts.py          starting a purchase: a checkout at the provider, recorded as an event, our ids attached;
#                         which provider a user pays through; changing a running plan
#   purchases.py          our rows kept in step with the provider: refreshing one, catching up what webhooks miss
#   webhooks.py           taking in the provider's webhooks (each refreshes a row through purchases.py)
#   subscriptions.py      acting on a purchase we have: ending it now, refunding it
#   access.py             what a user may do with a project, derived from what's been bought
#   polar.py              the provider's API (Polar, live and sandbox)
