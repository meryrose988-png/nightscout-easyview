"""Unified Dexcom + Medtrum uploader for Nightscout on Render web service."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)-7s - %(message)s')
logger = logging.getLogger(__name__)

PORT = int(os.getenv('PORT', '1337'))
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '180'))


def iso_from_unix(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def nightscout_headers(api_secret: str) -> dict[str, str]:
    return {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'api-secret': hashlib.sha1(api_secret.encode('utf-8')).hexdigest(),
    }


class NightscoutClient:
    def __init__(self, url: str, api_secret: str) -> None:
        self.url = url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update(nightscout_headers(api_secret))

    def close(self) -> None:
        self.session.close()

    def post_devicestatus(self, payload: dict[str, Any]) -> None:
        r = self.session.post(f'{self.url}/api/v1/devicestatus.json', json=payload, timeout=20)
        r.raise_for_status()

    def post_treatment(self, payload: dict[str, Any]) -> None:
        r = self.session.post(f'{self.url}/api/v1/treatments.json', json=payload, timeout=20)
        r.raise_for_status()


class MedtrumClient:
    BASE_URL = 'https://easyview.medtrum.eu/mobile/ajax'

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            'DevInfo': 'Android 12;Xiamoi vayu;Android 12',
            'AppTag': 'v=1.2.70(112);n=eyfo;p=android',
            'User-Agent': 'okhttp/3.5.0',
        })

    def close(self) -> None:
        self.session.close()

    def login(self) -> None:
        payload = {
            'apptype': 'Follow',
            'user_name': self.username,
            'password': self.password,
            'platform': 'google',
            'user_type': 'M',
        }
        r = self.session.post(f'{self.BASE_URL}/login', data=payload, timeout=20)
        r.raise_for_status()
        r.json()

    def status(self) -> dict[str, Any]:
        r = self.session.get(f'{self.BASE_URL}/logindata', timeout=20)
        r.raise_for_status()
        return r.json()


class DexcomClient:
    def __init__(self, api_secret: str) -> None:
        self.api_secret = api_secret
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Nightscout-Bridge/1.0'})

    def close(self) -> None:
        self.session.close()

    def fetch(self) -> dict[str, Any]:
        return {}


def build_medtrum_devicestatus(monitor_item: dict[str, Any]) -> dict[str, Any]:
    pump = monitor_item['pump_status']
    created_at = iso_from_unix(pump.get('updateTime'))
    return {
        'device': 'Medtrum EasyView',
        'created_at': created_at,
        'pump': {
            'clock': created_at,
            'reservoir': pump.get('remainingDose'),
            'iob': pump.get('iob'),
            'basal': pump.get('basalRate'),
            'status': pump.get('status'),
            'device': 'Medtrum',
        },
        'notes': f"Medtrum pump for {monitor_item.get('alias', 'unknown')}",
    }


def build_medtrum_bolus(monitor_item: dict[str, Any]) -> dict[str, Any] | None:
    pump = monitor_item['pump_status']
    bolus = pump.get('bolusDeliveried')
    bolus_time = pump.get('bolusDeliveriedTime')
    if bolus in (None, 0) or bolus_time is None:
        return None
    return {
        'eventType': 'Correction Bolus',
        'created_at': iso_from_unix(bolus_time),
        'insulin': float(bolus),
        'notes': f"Medtrum bolus for {monitor_item.get('alias', 'unknown')}",
    }


def run_medtrum_once(ns: NightscoutClient) -> None:
    username = os.getenv('MEDTRUM_USER')
    password = os.getenv('MEDTRUM_PASSWORD')
    if not username or not password:
        return
    c = MedtrumClient(username, password)
    try:
        c.login()
        status = c.status()
        monitorlist = status.get('monitorlist') or []
        if not monitorlist:
            logger.warning('no Medtrum monitorlist')
            return
        item = monitorlist[0]
        if 'pump_status' not in item:
            logger.warning('no Medtrum pump_status')
            return
        ns.post_devicestatus(build_medtrum_devicestatus(item))
        bolus = build_medtrum_bolus(item)
        if bolus:
            ns.post_treatment(bolus)
        logger.info('submitted Medtrum data to Nightscout')
    finally:
        c.close()


def run_dexcom_once(ns: NightscoutClient) -> None:
    api_secret = os.getenv('API_SECRET')
    if not api_secret:
        return
    c = DexcomClient(api_secret)
    try:
        _ = c.fetch()
        logger.info('dexcom sync placeholder executed')
    finally:
        c.close()


def worker_loop() -> None:
    ns_url = os.getenv('NS_URL') or os.getenv('NIGHTSCOUT_URL')
    ns_secret = os.getenv('NS_API_SECRET') or os.getenv('NIGHTSCOUT_API_SECRET') or os.getenv('API_SECRET')
    if not ns_url or not ns_secret:
        raise RuntimeError('Missing Nightscout env vars')

    ns = NightscoutClient(ns_url, ns_secret)
    try:
        while True:
            try:
                run_medtrum_once(ns)
            except Exception:
                logger.exception('Medtrum sync failed')
            try:
                run_dexcom_once(ns)
            except Exception:
                logger.exception('Dexcom sync failed')
            time.sleep(SYNC_INTERVAL)
    finally:
        ns.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'ok': True}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def main() -> None:
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    logger.info('listening on %s', PORT)
    server.serve_forever()


if __name__ == '__main__':
    main()
