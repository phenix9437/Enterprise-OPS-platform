"""Run the whole test-suite against a REAL PostgreSQL (a throwaway one bundled by `pgserver`).

    pip install pgserver
    cd backend && python -m scripts.test_on_postgres
"""
import os
import subprocess
import sys
import tempfile

import pgserver

if __name__ == "__main__":
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    try:
        env = {**os.environ, "TEST_DATABASE_URL": server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)}
        sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env))
    finally:
        server.cleanup()
