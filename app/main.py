"""FastAPI application entry point.

Run with:  uv run uvicorn app.main:app
or simply: ./run.sh
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .db import create_schema, get_engine
from .routes import api, pages

APP_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the SQLite file and tables on first start.

    The data imports (dictionary, frequency list) are separate scripts and are
    intentionally NOT run here: startup stays fast and predictable.
    """
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    create_schema(get_engine())
    yield


app = FastAPI(title="Wortgut", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

app.state.templates = Jinja2Templates(directory=APP_DIR / "templates")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

app.include_router(pages.router)
app.include_router(api.router)
