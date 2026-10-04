"""Job sources. Each module exposes parse(payload) and fetch(cfg, http_get) -> list[Posting]."""
from . import arbeitnow, ba, feeds

REGISTRY = {"arbeitnow": arbeitnow, "ba": ba, "feeds": feeds}
