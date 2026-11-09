"""My Day: owner goals decomposed into items with an estimated progress.

Owners: ``contract`` (contracts/myday_goals_v1.schema.json port), ``goals``
(records, owner commands, regeneration merge, views), ``evidence`` (linked
Coordinator request Workers), ``decompose`` (model suggestions) and ``service``
(HTTP composition with bounded in-flight work). The legacy generated daily
reports stay in ``lib.daily_report`` as the read-only day recap.
"""
