import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def boot_api_server(app) -> tuple[str, uvicorn.Server, threading.Thread]:
    """Boot a FastAPI app with uvicorn on an ephemeral port.

    Returns ``(base_url, server, thread)``. The caller must stop the server
    (``server.should_exit = True``) and join the thread when done.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "API server did not start in time"
    # uvicorn sets `started` in the serve loop; sockets land on `server.servers`
    # a moment later — wait for them before reading the ephemeral port.
    for _ in range(100):
        if server.servers:
            break
        time.sleep(0.05)
    assert server.servers, "API server never bound a socket"
    port = server.servers[0].sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", server, thread


@pytest.fixture(scope="session")
def api_server() -> Generator[str, None, None]:
    """One live FinSight API for the whole test session; yields its base URL."""
    from finance_agent.api import create_app

    base_url, server, thread = boot_api_server(create_app())
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
