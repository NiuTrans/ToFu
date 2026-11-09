"""Task flag for inbox carriers that no verified provider return has consumed.

The round drain (``orchestrator._swarm_inbox``) sets it when it appends an
inbox carrier (human steer, peer, swarm or background delivery) to the working
messages after the latest verified provider return; the verified-consumption
flush (``orchestrator._deferred_inbox_flush``) clears it; the model-replay
witness and the resume projection read it. Dependency-free, so storage-free
runs (the agent wheel) drain inboxes without importing storage owners.
"""

UNVERIFIED_INBOX_DELIVERY = '_inboxDeliveryUnverified'
