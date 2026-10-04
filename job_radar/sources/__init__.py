"""Job sources. Each module exposes parse(payload) and fetch(cfg, http_get) -> list[Posting]."""
from . import arbeitnow, ba

REGISTRY = {"arbeitnow": arbeitnow, "ba": ba}
