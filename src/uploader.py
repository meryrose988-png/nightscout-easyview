from __future__ import annotations

import functools
import hashlib
import logging
import os
import pathlib
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)


def with_retry(delay: int):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.ReadTimeout:
                    logger.info("Network timeout, retrying")
                except requests.exceptions.ConnectionError:
                    logger.info("Network connection error, retrying")
                time.sleep(delay)
        return wrapper
    return decorator


class EasyFollow:
    BASE_URL = "https://easyview.medtrum.eu/mobile/ajax"

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(
            {
                "DevInfo": "Android 12;Xiamoi vayu;Android 12",
                "AppTag": "v=1.2.70(112);n=eyfo;p=android",
                "User-Agent": "okhttp/3.5.0",
            }
        )

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @with_retry(delay=10)
    def _post(self, endpoint: str, data: dict) -> dict[str, Any]:
        response = self.session.post(f"{self.BASE_URL}/{endpoint}", data=data, timeout=10)
        response.raise_for_status()
        return response.json()

    @with_retry(delay=10)
    def _get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        response = self.session.get(f"{self.BASE_URL}/{endpoint}", params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def open(self) -> None:
        data = {
            "apptype": "Follow",
            "user_name": self.username,
            "password": self.password,
            "platform": "google",
            "user_type": "M",
        }
        self._post("login", data=data)
        logger.info("logged in to EasyView as %s", self.username)

    def close(self) -> None:
        logger.info("closed connection to EasyView")
        self.session.close()

    def get_status(self) -> dict[str, Any]:
        return self._get("logindata")

    def get_pump_payload(self) -> dict[str, Any] | None:
        raw_status = self.get_status()

        if raw_status.get("res") == "ERR":
            logger.error("EasyView API returned: %s", raw_status.get("msg"))
            return None

        monitorlist = raw_status.get("monitorlist", [])
        if len(monitorlist) != 1:
            logger.warning("Follower should have exactly one CGM user, got %i", len(monitorlist))
            return None

        monitor = monitorlist[0]
        logger.info("EasyView monitor payload: %s", monitor)

        device_name = (
            monitor.get("deviceType")
            or monitor.get("pumpDeviceType")
            or "Medtrum Pump"
        )

        clock_value = (
            monitor.get("pumpUpdateTime")
            or monitor.get("lastPumpDataTime")
            or monitor.get("updateTime")
        )

        if isinstance(clock_value, (int, float)):
            clock_iso = datetime.fromtimestamp(clock_value, tz=timezone.utc).isoformat()
        elif isinstance(clock_value, str):
            clock_iso = clock_value
        else:
            clock_iso = datetime.now(timezone.utc).isoformat()

        reservoir = (
            monitor.get("reservoir")
            or monitor.get("insulinLeft")
            or monitor.get("remainingInsulin")
            or monitor.get("pumpReservoir")
        )

        battery_raw = (
            monitor.get("pumpBatteryPercent")
            or monitor.get("batteryPercent")
            or monitor.get("pumpBattery")
            or monitor.get("battery")
        )

        status_text = (
            monitor.get("pumpStatus")
            or monitor.get("statusText")
            or monitor.get("status")
            or "unknown"
        )

        payload = {
            "device": device_name,
            "created_at": clock_iso,
            "pump": {
                "clock": clock_iso,
                "status": {
                    "status": str(status_text)
                }
            }
        }

        if reservoir is not None:
            try:
                payload["pump"]["reservoir"] = float(reservoir)
            except (TypeError, ValueError):
                logger.warning("invalid reservoir value: %r", reservoir)

        if battery_raw is not None:
            battery_obj: dict[str, Any] = {}
            try:
                battery_obj["percent"] = float(battery_raw)
            except (TypeError, ValueError):
                battery_obj["status"] = str(battery_raw)
            payload["pump"]["battery"] = battery_obj

        return payload


class NightScout:
    def __init__(self, url: str, api_secret: str):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-secret": hashlib.sha1(api_secret.encode("utf-8")).hexdigest(),
            }
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    @with_retry(delay=10)
    def add_devicestatus(self, payload: dict[str, Any]) -> None:
        response = self.session.post(
            f"{self.url}/api/v1/devicestatus.json",
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        logger.info("submitted pump status to nightscout")


def run_uploader():
    secrets_file = pathlib.Path(
        os.getenv(
            "SECRETS_FILE",
            str(pathlib.Path.home() / ".nightscout_easyview/secrets.yaml"),
        )
    )

    with secrets_file.open(encoding="utf-8") as f:
        secrets = yaml.safe_load(f)

    username = secrets["easyview"]["username"]
    password = secrets["easyview"]["password"]
    ns_url = secrets["nightscout"]["url"]
    api_secret = secrets["nightscout"]["secret"]

    last_payload_signature = None

    while True:
        try:
            with NightScout(ns_url, api_secret) as ns:
                with EasyFollow(username, password) as ef:
                    while True:
                        payload = ef.get_pump_payload()
                        if payload:
                            signature = str(payload)
                            if signature != last_payload_signature:
                                ns.add_devicestatus(payload)
                                last_payload_signature = signature
                            else:
                                logger.info("pump payload unchanged, skipping upload")
                        time.sleep(60)
        except Exception as e:
            logger.exception("pump uploader crashed, retrying in 30 seconds: %s", e)
            time.sleep(30)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def main():
    uploader_thread = threading.Thread(target=run_uploader, daemon=True)
    uploader_thread.start()

    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("HTTP health server listening on 0.0.0.0:%s", port)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)-7s - %(message)s",
    )
    main()
