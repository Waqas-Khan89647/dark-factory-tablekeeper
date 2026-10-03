"""Application wiring and the container entrypoint."""
from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from .errors import ApiError
from .http import api_error, http_error, unexpected_error
from .routes import routes


def create_app() -> Starlette:
    app = Starlette(routes=routes, exception_handlers={
        ApiError: api_error,
        HTTPException: http_error,
        Exception: unexpected_error,
    })
    app.router.redirect_slashes = False
    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8080),
                workers=1, log_level="warning", access_log=False,
                backlog=1024, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
