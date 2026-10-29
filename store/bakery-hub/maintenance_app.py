"""Temporary cutover-only WSGI app; deliberately imports no business application or DB."""


MESSAGE = "ただいまメンテナンス中です。しばらくしてから再度お試しください。".encode("utf-8")


def app(environ, start_response):
    # Reject every path and method, including login, Guest, static assets and
    # offline register status/write APIs. The server never reads a request body.
    start_response("503 Service Unavailable", [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(MESSAGE))),
        ("Cache-Control", "no-store"),
        ("Retry-After", "120"),
        ("X-Content-Type-Options", "nosniff"),
    ])
    return [] if environ.get("REQUEST_METHOD") == "HEAD" else [MESSAGE]
