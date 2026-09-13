"""Runtime configuration, read from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from notes_api.contract import DEFAULT_CONTRACT_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    contract_path: Path = DEFAULT_CONTRACT_PATH
