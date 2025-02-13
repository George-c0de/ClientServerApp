from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "vmdb"
    db_host: str = "localhost"
    db_port: int = 5432
    server_host: str = "0.0.0.0"
    server_port: int = 8888
    auth_password: str = "secret"

settings = AppSettings()
