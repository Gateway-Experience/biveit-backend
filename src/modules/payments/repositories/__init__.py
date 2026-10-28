# The payments module's own tables (migrations/0011_subscriptions.sql, 0012_orders.sql): our copy of what the
# payment provider says was bought. Written only from the provider's answers (services/webhooks.py); everything
# else reads.
#   subscriptions.py  plans and domains, recurring
#   orders.py         one-off purchases
