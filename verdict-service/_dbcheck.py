import os, sys, logging
log = logging.getLogger("dbcheck")
def validate_database_url(url: str | None, *, required: bool) -> None:
    """Fail fast on an obviously-wrong DATABASE_URL."""
    if not url:
        if required:
            log.error("DATABASE_URL is not set — this service cannot start."); sys.exit(1)
        return
    if not (url.startswith("postgres://") or url.startswith("postgresql://")):
        log.error("DATABASE_URL is not a Postgres DSN (starts with %r). "
                  "This looks like the wrong value (e.g. a service URL) was pasted in. Refusing to start.",
                  url[:16]); sys.exit(1)
    if "/parser/" in url or "/billing/" in url or "http" in url:
        log.error("DATABASE_URL contains an HTTP/service-path fragment — wrong value pasted. Refusing to start."); sys.exit(1)
