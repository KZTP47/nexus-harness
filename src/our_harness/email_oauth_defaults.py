"""Release-owned desktop OAuth registrations, shared by all new projects.

The publisher fills these public client identifiers after registering Nexus
with each provider. Empty values deliberately keep sign-in unavailable rather
than borrowing another application's identity. No mailbox token belongs here.
"""

SCHEMA_VERSION = 1
PUBLISHER_REGISTRATIONS = {
    'outlook': {'client_id': '', 'tenant': 'common'},
    'gmail': {'client_id': ''},
}
