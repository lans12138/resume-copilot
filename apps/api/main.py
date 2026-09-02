"""ASGI entry point used by Uvicorn."""

from backend.app.main import create_app

app = create_app()
