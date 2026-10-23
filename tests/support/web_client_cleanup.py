"""Release HTTP clients and the Web sessions owned by class-scoped tests."""


def close_web_test_client(client) -> None:
    """Drain session workers before closing a client created outside test fixtures."""
    try:
        completed = client.app.state.web_session_registry.shutdown_all(
            wait=True,
            timeout=5.0,
        )
        if completed is not True:
            raise AssertionError("test Web sessions did not finish shutting down")
    finally:
        client.close()
