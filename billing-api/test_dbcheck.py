"""Fail-fast DATABASE_URL validation (shared _dbcheck helper)."""
import pytest
from _dbcheck import validate_database_url


def test_valid_postgres_dsn_does_not_exit():
    validate_database_url("postgresql://user:pw@host:5432/db", required=True)     # no raise
    validate_database_url("postgres://user:pw@host:5432/db", required=True)


def test_http_url_exits():
    with pytest.raises(SystemExit):
        validate_database_url("https://verdict-service.up.railway.app", required=True)


def test_service_path_fragment_exits():
    # Postgres-prefixed so it passes the scheme check and specifically trips the
    # "/parser/" service-path guard.
    with pytest.raises(SystemExit):
        validate_database_url("postgresql://host:5432/parser/verdict", required=True)


def test_none_required_exits():
    with pytest.raises(SystemExit):
        validate_database_url(None, required=True)


def test_none_not_required_does_not_exit():
    validate_database_url(None, required=False)      # no raise
