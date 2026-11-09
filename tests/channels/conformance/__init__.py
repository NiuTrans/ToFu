"""Conformance suite of the channel protocol (docs/CHANNEL_PROTOCOL.md, Conformance).

``harness`` holds the compatibility shim and the driver base, ``reference``
the protocol-native reference adapter and its fake platform, ``drivers``
today's adapters against their platform fakes, ``cases`` the protocol cases,
``baseline.json`` which case each adapter passes today, and
``test_conformance`` runs every case against every adapter under that
baseline (strict expected failures).
"""
