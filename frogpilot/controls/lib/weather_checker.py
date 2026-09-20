#!/usr/bin/env python3
import requests
import time

from concurrent.futures import ThreadPoolExecutor

from openpilot.common.params import Params

from openpilot.frogpilot.common.frogpilot_api import FrogPilotAPIError
from openpilot.frogpilot.common.frogpilot_utilities import calculate_distance_to_point

CACHE_DISTANCE = 25_000
CACHE_MAX_AGE = 20 * 60
UPDATE_DISTANCE = 20_000

DEFAULT_UPDATE_INTERVAL = 15 * 60
PERSONAL_KEY_UPDATE_INTERVAL = 60

RETRY_INTERVAL = 60

# Reference: https://openweathermap.org/weather-conditions
WEATHER_CATEGORIES = {
  "RAIN": {
    "ranges": [(300, 321), (500, 504)],
    "suffix": "rain",
  },
  "RAIN_STORM": {
    "ranges": [(200, 232), (511, 511), (520, 531), (771, 771), (781, 781)],
    "suffix": "rain_storm",
  },
  "SNOW": {
    "ranges": [(600, 622)],
    "suffix": "snow",
  },
  "LOW_VISIBILITY": {
    "ranges": [(701, 762)],
    "suffix": "low_visibility",
  },
  "CLEAR": {
    "ranges": [(800, 800)],
    "suffix": "clear",
  },
}

WEATHER_OFFSETS = (
  "increase_following_distance",
  "increase_stopped_distance",
  "reduce_acceleration",
  "reduce_lateral_acceleration",
)


def weather_category(weather_id):
  return next(
    (category["suffix"] for category in WEATHER_CATEGORIES.values() if any(start <= weather_id <= end for start, end in category["ranges"])),
    "unknown",
  )


class WeatherChecker:
  def __init__(self, frogpilot_api):
    self.params = Params()

    self.is_daytime = False
    self.request_valid = False

    self.api_25_calls = 0
    self.api_3_calls = 0
    self.api_4_calls = 0
    self.increase_following_distance = 0
    self.increase_stopped_distance = 0
    self.next_request = 0
    self.next_retry = 0
    self.reduce_acceleration = 0
    self.reduce_lateral_acceleration = 0
    self.sunrise = 0
    self.sunset = 0
    self.weather_id = 0

    self.last_position = None
    self.last_request_time = None
    self.personal_api_key = None
    self.future = None
    self.request_position = None
    self.request_time = 0

    self.personal_api_version = "4.0"

    self.frogpilot_api = frogpilot_api

    self.executor = ThreadPoolExecutor(max_workers=1)

    self.session = requests.Session()

  def close(self):
    self.invalidate()
    self.executor.submit(self.session.close)
    self.executor.shutdown(wait=False)

  def update_offsets(self, frogpilot_toggles):
    category = weather_category(self.weather_id)
    for offset in WEATHER_OFFSETS:
      value = getattr(frogpilot_toggles, f"{offset}_{category}") if category not in ("clear", "unknown") else 0
      setattr(self, offset, value)

  def invalidate(self, discard_request=True):
    if discard_request:
      self.request_valid = False

    self.last_position = None
    self.last_request_time = None
    self.next_request = 0
    self.weather_id = 0
    self.sunrise = 0
    self.sunset = 0
    self.is_daytime = False
    for offset in WEATHER_OFFSETS:
      setattr(self, offset, 0)

  def update_weather(self, gps_position, now, frogpilot_toggles):
    timestamp = time.monotonic()

    position = (gps_position["latitude"], gps_position["longitude"])

    if self.future is not None and self.future.done():
      self.complete_request(position, timestamp)

    distance = calculate_distance_to_point(*self.last_position, *position) if self.last_position is not None else 0
    if self.last_request_time is not None and (timestamp - self.last_request_time >= CACHE_MAX_AGE or distance >= CACHE_DISTANCE):
      # Expiring the previous result must not discard a refresh already in flight.
      self.invalidate(discard_request=False)

    self.is_daytime = self.sunrise <= now.timestamp() < self.sunset

    self.update_offsets(frogpilot_toggles)

    if self.future is not None or timestamp < self.next_retry:
      return

    moved = distance >= UPDATE_DISTANCE
    if timestamp < self.next_request and not moved:
      return

    api_key = self.params.get("WeatherToken")

    self.request_valid = True
    self.request_position = position
    self.request_time = timestamp

    self.next_retry = timestamp + RETRY_INTERVAL

    def make_request():
      if self.personal_api_key != api_key:
        self.personal_api_key = api_key
        self.personal_api_version = "4.0"

      if api_key:
        endpoints = {"4.0": "4.0/onecall/current", "3.0": "3.0/onecall", "2.5": "2.5/weather"}
        api_versions = tuple(endpoints)
        try:
          for api_version in api_versions[api_versions.index(self.personal_api_version):]:
            query = {"appid": api_key, "lat": position[0], "lon": position[1]}
            if api_version == "3.0":
              query["exclude"] = "alerts,daily,hourly,minutely"

            url = f"https://api.openweathermap.org/data/{endpoints[api_version]}"
            with self.session.get(url, params=query, timeout=30, allow_redirects=False) as response:
              if response.status_code in (401, 403, 404, 410):
                continue
              if not 200 <= response.status_code < 300:
                break
              data = response.json()

            if api_version == "4.0":
              data = data["data"][0]
              sun_data = data
            elif api_version == "3.0":
              data = data["current"]
              sun_data = data
            else:
              sun_data = data["sys"]

            if not isinstance(sun_data, dict):
              raise ValueError("Invalid personal weather data")

            weather = {
              "api_version": api_version,
              "sunrise": sun_data.get("sunrise", 0),
              "sunset": sun_data.get("sunset", 0),
              "using_personal_key": True,
              "weather_id": data["weather"][0]["id"],
            }
            if not all(type(weather[key]) is int for key in ("sunrise", "sunset", "weather_id")):
              raise ValueError("Invalid personal weather data")

            self.personal_api_version = api_version
            return weather
        except (IndexError, KeyError, TypeError, ValueError, requests.RequestException):
          pass

      return self.frogpilot_api.post_json("/v1/weather", {"latitude": position[0], "longitude": position[1]}, session=self.session, timeout=30)

    self.future = self.executor.submit(make_request)

  def complete_request(self, position, timestamp):
    future = self.future
    self.future = None

    try:
      data = future.result()
    except (FrogPilotAPIError, IndexError, KeyError, TypeError, ValueError, requests.RequestException):
      return

    if not self.request_valid or timestamp - self.request_time >= CACHE_MAX_AGE:
      return
    if calculate_distance_to_point(*self.request_position, *position) >= CACHE_DISTANCE:
      return

    if not isinstance(data, dict) or not isinstance(data.get("api_version"), str):
      return
    if not all(type(data.get(key)) is int for key in ("sunrise", "sunset", "weather_id")):
      return

    using_personal_key = data.get("using_personal_key", False)
    if not isinstance(using_personal_key, bool):
      return

    if data.get("api_version") == "2.5":
      self.api_25_calls += 1
    elif data.get("api_version") == "3.0":
      self.api_3_calls += 1
    elif data.get("api_version") == "4.0":
      self.api_4_calls += 1

    self.last_position = self.request_position
    self.last_request_time = self.request_time
    self.next_request = self.request_time + (PERSONAL_KEY_UPDATE_INTERVAL if using_personal_key else DEFAULT_UPDATE_INTERVAL)

    self.sunrise = data["sunrise"]
    self.sunset = data["sunset"]
    self.weather_id = data["weather_id"]
