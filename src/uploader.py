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
            logger.warning("Follower should have exactly one monitored user, got %i", len(monitorlist))
            return None

        monitor = monitorlist[0]
        pump_status = monitor.get("pump_status")

        if not pump_status:
            logger.warning("no pump_status found in EasyView account")
            return None

        logger.info("EasyView pump payload: %s", pump_status)

        update_time = pump_status.get("updateTime")
        if isinstance(update_time, (int, float)):
            clock_iso = datetime.fromtimestamp(update_time, tz=timezone.utc).isoformat()
        else:
            clock_iso = datetime.now(timezone.utc).isoformat()

        payload = {
            "device": f"Medtrum Pump {pump_status.get('serial', '')}".strip(),
            "created_at": clock_iso,
            "pump": {
                "clock": clock_iso,
                "reservoir": float(pump_status["remainingDose"]),
                "status": {
                    "status": str(pump_status.get("status", "unknown")),
                },
            },
        }

        if "batteryPercent" in pump_status:
            payload["pump"]["battery"] = {"percent": float(pump_status["batteryPercent"])}

        return payload

    def get_bolus_event(self) -> dict[str, Any] | None:
        raw_status = self.get_status()

        if raw_status.get("res") == "ERR":
            logger.error("EasyView API returned: %s", raw_status.get("msg"))
            return None

        monitorlist = raw_status.get("monitorlist", [])
        if len(monitorlist) != 1:
            logger.warning("Follower should have exactly one monitored user, got %i", len(monitorlist))
            return None

        monitor = monitorlist[0]
        pump_status = monitor.get("pump_status")
        if not pump_status:
            return None

        delivered = pump_status.get("bolusDeliveried")
        delivered_time = pump_status.get("bolusDeliveriedTime")
        if delivered is None or delivered_time is None:
            return None

        try:
            insulin = float(delivered)
            ts = int(delivered_time)
        except (TypeError, ValueError):
            return None

        if insulin <= 0:
            return None

        return {
            "insulin": round(insulin, 2),
            "created_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "bolus_time": ts,
            "bolus_key": f"{ts}:{round(insulin, 2)}",
        }


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

    @with_retry(delay=10)
    def add_treatment(self, payload: dict[str, Any]) -> None:
        response = self.session.post(
            f"{self.url}/api/v1/treatments.json",
            json={
                "eventType": "Correction Bolus",
                "created_at": payload["created_at"],
                "insulin": payload["insulin"],
                "enteredBy": "nightscout-easyview",
                "notes": f"EasyView bolus {payload['insulin']}U",
            },
            timeout=15,
        )
        response.raise_for_status()
        logger.info("submitted bolus treatment to nightscout")


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
    last_bolus_key = None

    while True:
        try:
            with NightScout(ns_url, api_secret) as ns:
                with EasyFollow(username, password) as ef:
                    while True:
                        pump_payload = ef.get_pump_payload()
                        if pump_payload:
                            signature = str(pump_payload)
                            if signature != last_payload_signature:
                                ns.add_devicestatus(pump_payload)
                                last_payload_signature = signature
                            else:
                                logger.info("pump payload unchanged, skipping upload")

                        bolus_event = ef.get_bolus_event()
                        if bolus_event:
                            if bolus_event["bolus_key"] != last_bolus_key:
                                ns.add_treatment(bolus_event)
                                last_bolus_key = bolus_event["bolus_key"]
                            else:
                                logger.info("bolus already sent, skipping")

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
