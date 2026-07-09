import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings


logger = logging.getLogger(__name__)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PREVIOUS_DAY_MARKERS = (
    "\u6628\u5929",
    "\u6628\u65e5",
    "\u6628\u665a",
    "\u524d\u4e00\u5929",
    "\u524d\u665a",
)

RAIN_WEATHER_CODES = {
    51,
    53,
    55,
    56,
    57,
    61,
    63,
    65,
    66,
    67,
    80,
    81,
    82,
    95,
    96,
    99,
}


def _first_number(values: list[Any] | None) -> float | None:
    if not values:
        return None
    value = values[0]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_int(values: list[Any] | None) -> int | None:
    value = _first_number(values)
    return int(value) if value is not None else None


def _number_at(values: list[Any] | None, index: int) -> float | None:
    if not values or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_at(values: list[Any] | None, index: int) -> int | None:
    value = _number_at(values, index)
    return int(value) if value is not None else None


def _parse_message_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(SHANGHAI_TZ)

    text = str(value).strip()
    try:
        timestamp = int(text)
        seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        return datetime.fromtimestamp(seconds, SHANGHAI_TZ)
    except (TypeError, ValueError, OSError):
        pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(SHANGHAI_TZ)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def _daypart(moment: datetime) -> str:
    if moment.hour < 11:
        return "morning"
    if moment.hour < 18:
        return "daytime"
    return "evening"


def _is_rain_likely(precipitation_probability: float | None, weather_code: int | None) -> bool:
    return (precipitation_probability is not None and precipitation_probability >= 50) or weather_code in RAIN_WEATHER_CODES


def _is_previous_day_activity(user_text: str) -> bool:
    return any(marker in user_text for marker in PREVIOUS_DAY_MARKERS)


def _rain_path_text(part: str) -> str:
    if part == "morning":
        return "\u65e9\u4e0a\u7ec3\u5b8c\u53bb\u4e0a\u73ed\u8def\u4e0a\u6ce8\u610f\u9632\u6ed1\u548c\u5b89\u5168\uff0c\u96e8\u5929\u522b\u8ddf\u8def\u9762\u62fc\u914d\u901f"
    if part == "evening":
        return "\u665a\u4e0a\u7ec3\u5b8c\u56de\u5bb6\u8def\u4e0a\u6ce8\u610f\u9632\u6ed1\u548c\u5b89\u5168\uff0c\u522b\u4e3a\u4e86\u6253\u5361\u628a\u81ea\u5df1\u6dcb\u900f"
    return "\u8bad\u7ec3\u8def\u4e0a\u6ce8\u610f\u9632\u6ed1\u548c\u5b89\u5168\uff0c\u96e8\u5929\u522b\u592a\u8d76"


def _heat_path_text(part: str) -> str:
    if part == "morning":
        return "\u65e9\u4e0a\u7ec3\u5b8c\u53bb\u4e0a\u73ed\u522b\u5fd8\u4e86\u8865\u6c34\uff0c\u522b\u4e00\u8def\u70ed\u6210\u5c0f\u756a\u8304"
    if part == "evening":
        return "\u665a\u4e0a\u7ec3\u4e5f\u522b\u592a\u4e0a\u5934\uff0c\u56de\u5bb6\u8def\u4e0a\u6162\u70b9\u513f\uff0c\u8bb0\u5f97\u8865\u6c34"
    return "\u6237\u5916\u8bad\u7ec3\u522b\u786c\u9876\uff0c\u5c3d\u91cf\u907f\u5f00\u4e2d\u5348\uff0c\u5f3a\u5ea6\u6536\u4e00\u70b9\u3001\u591a\u8865\u6c34"


def _future_rain_tip(
    city_name: str,
    future_precipitation_probabilities: list[float | None],
    future_weather_codes: list[int | None],
) -> str:
    rainy_offsets: list[int] = []
    for index, (probability, code) in enumerate(zip(future_precipitation_probabilities, future_weather_codes, strict=False), start=1):
        if _is_rain_likely(probability, code):
            rainy_offsets.append(index)
    if not rainy_offsets:
        return ""

    day_text = "\u660e\u5929" if rainy_offsets[0] == 1 else "\u540e\u5929"
    if len(rainy_offsets) >= 2:
        day_text = "\u672a\u6765\u4e24\u5929"
    return f"\u987a\u624b\u63d0\u524d\u6253\u4e2a\u9884\u544a\uff1a{city_name}{day_text}\u53ef\u80fd\u8fd8\u6709\u96e8\uff0c\u5305\u91cc\u8bb0\u5f97\u585e\u628a\u4f1e \U0001f302"


def build_weather_training_tip(
    city: str,
    max_temperature_celsius: float | None,
    precipitation_probability: float | None,
    weather_code: int | None,
    high_temp_celsius: float = 32.0,
    message_time: datetime | None = None,
    is_previous_day_activity: bool = False,
    future_precipitation_probabilities: list[float | None] | None = None,
    future_weather_codes: list[int | None] | None = None,
) -> str:
    tips: list[str] = []
    city_name = city or "Beijing"
    part = _daypart(message_time or datetime.now(SHANGHAI_TZ))
    if max_temperature_celsius is not None and max_temperature_celsius >= high_temp_celsius:
        tips.append(
            f"\u5929\u6c14\u63d0\u9192\uff1a{city_name}\u4eca\u5929\u6700\u9ad8\u6e29\u7ea6 {max_temperature_celsius:.0f}C\uff0c"
            f"{_heat_path_text(part)} \U0001f31e"
        )

    if _is_rain_likely(precipitation_probability, weather_code):
        probability_text = f"{precipitation_probability:.0f}%" if precipitation_probability is not None else "\u8f83\u9ad8"
        tips.append(
            f"\u5929\u6c14\u63d0\u9192\uff1a{city_name}\u4eca\u5929\u6709\u964d\u96e8\u98ce\u9669\uff08\u6982\u7387\u7ea6 {probability_text}\uff09\uff0c"
            f"{_rain_path_text(part)} \U0001f327"
        )

    if is_previous_day_activity:
        future_tip = _future_rain_tip(city_name, future_precipitation_probabilities or [], future_weather_codes or [])
        if future_tip:
            tips.append(future_tip)

    return "\n".join(tips[:2])


async def get_weather_training_tip(settings: Settings, message_created_at: str = "", user_text: str = "") -> str:
    if not settings.weather_enabled:
        return ""

    try:
        async with httpx.AsyncClient(timeout=settings.weather_timeout_seconds) as client:
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": settings.weather_latitude,
                    "longitude": settings.weather_longitude,
                    "daily": "temperature_2m_max,precipitation_probability_max,weather_code",
                    "forecast_days": 3,
                    "timezone": "Asia/Shanghai",
                },
            )
        if response.status_code >= 400:
            logger.warning("Weather request failed: status=%s body=%s", response.status_code, response.text[:500])
            return ""

        daily = response.json().get("daily", {})
        return build_weather_training_tip(
            settings.weather_city,
            _first_number(daily.get("temperature_2m_max")),
            _first_number(daily.get("precipitation_probability_max")),
            _first_int(daily.get("weather_code")),
            settings.weather_high_temp_celsius,
            _parse_message_datetime(message_created_at),
            _is_previous_day_activity(user_text),
            [_number_at(daily.get("precipitation_probability_max"), index) for index in (1, 2)],
            [_int_at(daily.get("weather_code"), index) for index in (1, 2)],
        )
    except Exception:
        logger.exception("Weather request failed unexpectedly")
        return ""
