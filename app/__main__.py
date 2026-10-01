import uvicorn

from app.config import get_settings
from app.security import check_bind_is_safe


def main() -> None:
    settings = get_settings()
    check_bind_is_safe()
    tls = {}
    if settings.ssl_certfile and settings.ssl_keyfile:
        tls = {"ssl_certfile": str(settings.ssl_certfile), "ssl_keyfile": str(settings.ssl_keyfile)}
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="warning", access_log=False, **tls)


if __name__ == "__main__":
    main()
