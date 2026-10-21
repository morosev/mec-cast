"""The middleware these e2e tests run on: MIDDLEWARE / MIDDLEWARE_LINK, as
`make test-e2e` and the CI matrix set them (ADR-0013). Imported first by every
test module, so compose starts the matching centre (COMPOSE_PROFILES)."""

import os

MIDDLEWARE = os.environ.setdefault("MIDDLEWARE", "zenoh")
os.environ.setdefault("COMPOSE_PROFILES", MIDDLEWARE)
#: The edge's star centre: what every log dump should include.
CENTRE = "dds-discovery" if MIDDLEWARE == "dds" else "zenoh-router"
