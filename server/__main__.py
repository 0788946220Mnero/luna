"""تشغيل الخادم: python -m server"""
import uvicorn

from codeagent.env_config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
