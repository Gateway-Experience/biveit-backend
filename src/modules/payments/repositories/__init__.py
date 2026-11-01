# The payments module's own table (migrations/0011_subscriptions.sql): our copy of what the payment provider says
# was bought. Written only from the provider's answers (services/webhooks.py); everything else reads.
#   subscriptions.py  everything bought: plans and domains (recurring), the one-time purchase (lifetime)
