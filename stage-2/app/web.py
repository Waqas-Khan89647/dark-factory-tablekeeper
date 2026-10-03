"""The four required HTML screens (stage-2.md UI) and their static assets.

Every page is a static file: the browser does all rendering and API calls client-side
in `static/app.js`, so these routes just serve bytes already baked into the image.
"""
from __future__ import annotations

import pathlib

from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

PAGES = pathlib.Path(__file__).resolve().parent / "web" / "pages"
STATIC = pathlib.Path(__file__).resolve().parent / "web" / "static"


def _page(name: str):
    path = PAGES / name

    async def handler(request):
        return FileResponse(path, media_type="text/html; charset=utf-8")

    return handler


web_routes = [
    Route("/", _page("index.html"), methods=["GET"]),
    Route("/signup", _page("signup.html"), methods=["GET"]),
    Route("/login", _page("login.html"), methods=["GET"]),
    Route("/lookup", _page("lookup.html"), methods=["GET"]),
    Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
]
