"""Entry point for Railway deployment."""

import os
import uvicorn

from src.server import app  # noqa: F401 — re-exported for uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False,
    )
