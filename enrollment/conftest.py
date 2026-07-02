"""
Test setup: DATABASE_URL is now validated at import (validate_database_url,
required=True), so `import app` needs a valid-looking DSN. Set a dummy Postgres
DSN here — no test ever connects (the storage fixtures force InMemoryStorage).
"""
import os

os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/testdb"
