from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import create_tables
from .webhook import router as webhook_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    yield


app = FastAPI(title="Concilia", version="0.1.0", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
