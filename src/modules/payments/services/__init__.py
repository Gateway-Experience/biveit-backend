# The payments module's services:
#   catalog.py            what we sell (plans, domain prices, provider variants) -- pure, off the constants
#   checkouts.py          starting a purchase: a checkout link at the provider, our ids attached
#   webhooks.py           taking in the provider's webhooks into our tables
#   access.py             what a user may do with a project, derived from what's been bought
#   lemonsqueezy.py       the provider's API (mock_lemonsqueezy.py stands in for it with payments_mock on)
