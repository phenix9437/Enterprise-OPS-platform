"""Run the whole test-suite against REAL infrastructure: a throwaway PostgreSQL (via `pgserver`) and, if
`redislite` is installed, a real Redis server. That also switches on the Celery integration tests, which start
a real Celery worker process.

    pip install pgserver redislite
    cd backend && python -m scripts.test_on_postgres
"""
import os
import socket
import subprocess
import sys
import tempfile
import time

import pgserver


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_redis():
    try:
        import redislite
    except ImportError:
        print("redislite not installed: skipping the Redis/Celery integration tests")
        return None, None
    port = free_port()
    proc = subprocess.Popen([redislite.__redis_executable__, "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis

    for _ in range(50):
        try:
            if redis.Redis(port=port).ping():
                return proc, f"redis://127.0.0.1:{port}/0"
        except redis.ConnectionError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("could not start Redis")


if __name__ == "__main__":
    server = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    redis_proc, redis_url = start_redis()
    try:
        env = {**os.environ, "TEST_DATABASE_URL": server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)}
        if redis_url:
            env["TEST_REDIS_URL"] = redis_url
        sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env))
    finally:
        if redis_proc:
            redis_proc.terminate()
        server.cleanup()
