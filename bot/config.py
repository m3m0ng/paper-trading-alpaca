from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

PAPER_URL = "https://paper-api.alpaca.markets"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read simple KEY=value settings without executing the .env file as shell code."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    value = values.get(key, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{key} must be true or false")
    return value == "true"


def _positive_float(values: dict[str, str], key: str, default: float) -> float:
    value = float(values.get(key, default))
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    if value < 0:
        raise ValueError(f"{key} cannot be negative")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret_key: str
    api_base_url: str
    trading_enabled: bool
    max_managed_equity: float
    cash_buffer_percent: float
    target_annual_volatility: float
    ai_enabled: bool
    ai_monthly_budget_usd: float

    def __post_init__(self) -> None:
        for key, value in {
            "BOT_TRADING_ENABLED": self.trading_enabled,
            "BOT_AI_ENABLED": self.ai_enabled,
        }.items():
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")

        if self.api_base_url != PAPER_URL:
            raise ValueError("This bot is paper-only: APCA_API_BASE_URL must be https://paper-api.alpaca.markets")

        numeric_values = {
            "BOT_MAX_MANAGED_EQUITY": self.max_managed_equity,
            "BOT_CASH_BUFFER_PERCENT": self.cash_buffer_percent,
            "BOT_TARGET_ANNUAL_VOLATILITY": self.target_annual_volatility,
            "BOT_AI_MONTHLY_BUDGET_USD": self.ai_monthly_budget_usd,
        }
        for key, value in numeric_values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be finite")

        if self.max_managed_equity < 0:
            raise ValueError("BOT_MAX_MANAGED_EQUITY cannot be negative")
        if not 0 <= self.cash_buffer_percent < 100:
            if self.cash_buffer_percent < 0:
                raise ValueError("BOT_CASH_BUFFER_PERCENT cannot be negative")
            raise ValueError("BOT_CASH_BUFFER_PERCENT must be below 100")
        if not 0 < self.target_annual_volatility <= 1:
            raise ValueError("BOT_TARGET_ANNUAL_VOLATILITY must be between 0 and 1")
        if self.ai_monthly_budget_usd < 0:
            raise ValueError("BOT_AI_MONTHLY_BUDGET_USD cannot be negative")

    @classmethod
    def load(cls, env_path: Path = Path(".env")) -> "Settings":
        values = _read_dotenv(env_path)
        api_base_url = values.get("APCA_API_BASE_URL", PAPER_URL).rstrip("/")

        settings = cls(
            api_key=values.get("APCA_API_KEY_ID", ""),
            secret_key=values.get("APCA_API_SECRET_KEY", ""),
            api_base_url=api_base_url,
            trading_enabled=_bool(values, "BOT_TRADING_ENABLED"),
            max_managed_equity=_positive_float(values, "BOT_MAX_MANAGED_EQUITY", 0),
            cash_buffer_percent=_positive_float(values, "BOT_CASH_BUFFER_PERCENT", 2),
            target_annual_volatility=_positive_float(values, "BOT_TARGET_ANNUAL_VOLATILITY", 0.10),
            ai_enabled=_bool(values, "BOT_AI_ENABLED"),
            ai_monthly_budget_usd=_positive_float(values, "BOT_AI_MONTHLY_BUDGET_USD", 0),
        )
        if not settings.api_key or not settings.secret_key:
            raise ValueError("APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set in .env")
        if settings.trading_enabled and settings.max_managed_equity <= 0:
            raise ValueError("Set BOT_MAX_MANAGED_EQUITY above zero before enabling execution")
        return settings
