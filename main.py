"""Точка входа FB Master. Не трогает существующий app.py (парсер на порту 8765)."""

from backend.app_factory import create_app
from backend.config import BO_HOST, BO_PORT

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=BO_HOST, port=BO_PORT, reload=True)
