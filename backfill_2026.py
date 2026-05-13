#!/usr/bin/env python3
"""Однократная загрузка всех ГОСТ-уведомлений 2026 года из API ФГИС.

Загружает все записи по каждому статусу с фильтром по дате
и сохраняет в data/dashboard_registry.json.

Скрипт идемпотентен — повторный запуск обновит данные без дублирования.

Использование:
    python backfill_2026.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime

# Загрузка .env
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

import requests

# Импорт из проекта — активирует DNS-патч при загрузке модуля
from src.gost_scraper import _EMPTY_FILTERS, _parse_api_row
from src.gost_config import GOST_API_URL, GOST_API_IPS
from src.dashboard_config import (
    DASHBOARD_REGISTRY_PATH,
    ALL_GOST_STATUSES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Формат даты для API (ISO)
DATE_FROM = "2026-01-01"
BATCH_SIZE = 20
REQUEST_DELAY = 0.5  # секунд между запросами


def _load_registry() -> dict:
    """Загружает или создаёт пустой реестр."""
    if os.path.exists(DASHBOARD_REGISTRY_PATH):
        with open(DASHBOARD_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "metadata": {
            "last_updated": "",
            "gost_count": 0,
            "sp_count": 0,
            "last_backfill": "",
        },
        "gost": [],
        "sp": [],
    }


def _save_registry(registry: dict) -> None:
    """Сохраняет реестр в JSON."""
    os.makedirs(os.path.dirname(DASHBOARD_REGISTRY_PATH), exist_ok=True)
    with open(DASHBOARD_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def _create_session() -> requests.Session:
    """Создаёт HTTP-сессию."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


def fetch_all_gost_2026(session: requests.Session) -> list[dict]:
    """Загружает все ГОСТ-уведомления 2026 года по всем статусам."""
    all_records = []
    today = datetime.now().strftime("%Y-%m-%d")

    for status in ALL_GOST_STATUSES:
        logger.info(f"--- Статус: {status} ---")

        # Узнаём количество страниц
        params = {
            **_EMPTY_FILTERS,
            "submittedPublicDiscussionDate": DATE_FROM,
            "statusDocumentNDS": status,
            "page": 1,
            "rows": BATCH_SIZE,
        }

        try:
            resp = session.get(GOST_API_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Ошибка запроса API для статуса '{status}': {e}")
            continue

        total_pages_raw = data.get("total", "0")
        total_pages = int(
            str(total_pages_raw).replace(" ", "").replace("\xa0", "")
        )
        total_records = data.get("records", "0")
        logger.info(f"Найдено: {total_records} записей на {total_pages} стр.")

        if total_pages == 0:
            continue

        # Обрабатываем первую страницу
        for row in data.get("rows", []):
            notification = _parse_api_row(row)
            if notification:
                notification["fetched_date"] = today
                notification["source"] = "gost"
                all_records.append(notification)

        # Загружаем остальные страницы
        for page_num in range(2, total_pages + 1):
            if page_num % 10 == 0 or page_num == total_pages:
                logger.info(f"  Страница {page_num}/{total_pages}...")

            time.sleep(REQUEST_DELAY)
            params["page"] = page_num

            try:
                resp = session.get(GOST_API_URL, params=params, timeout=30)
                resp.raise_for_status()
                page_data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logger.error(f"  Ошибка загрузки стр. {page_num}: {e}")
                continue

            for row in page_data.get("rows", []):
                notification = _parse_api_row(row)
                if notification:
                    notification["fetched_date"] = today
                    notification["source"] = "gost"
                    all_records.append(notification)

    logger.info(f"Всего загружено: {len(all_records)} записей")
    return all_records


def backfill():
    """Основная функция загрузки данных."""
    logger.info("=== Начало загрузки ГОСТ-уведомлений 2026 ===")

    session = _create_session()
    records = fetch_all_gost_2026(session)

    if not records:
        logger.warning("Не удалось загрузить записи.")
        return 1

    # Загружаем существующий реестр и мержим
    registry = _load_registry()
    existing_ids = {r["id"] for r in registry["gost"]}

    new_count = 0
    updated_count = 0
    for rec in records:
        if rec["id"] not in existing_ids:
            registry["gost"].append(rec)
            existing_ids.add(rec["id"])
            new_count += 1
        else:
            # Обновляем статус существующей записи
            for existing in registry["gost"]:
                if existing["id"] == rec["id"]:
                    if existing.get("status") != rec.get("status"):
                        existing["status"] = rec["status"]
                        updated_count += 1
                    # Обновляем end_date если появилась
                    if rec.get("end_date") and not existing.get("end_date"):
                        existing["end_date"] = rec["end_date"]
                    break

    # Обновляем метаданные
    now = datetime.now().isoformat()
    registry["metadata"]["last_updated"] = now
    registry["metadata"]["gost_count"] = len(registry["gost"])
    registry["metadata"]["last_backfill"] = now

    _save_registry(registry)

    logger.info(
        f"=== Загрузка завершена: "
        f"{new_count} новых, {updated_count} обновлённых, "
        f"всего в реестре: {len(registry['gost'])} ГОСТов ==="
    )
    return 0


if __name__ == "__main__":
    sys.exit(backfill())
