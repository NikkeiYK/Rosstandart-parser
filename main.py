#!/usr/bin/env python3
"""Мониторинг уведомлений Росстандарта:
  1) Своды правил (СП) — с rst.gov.ru
  2) ГОСТы (публичные обсуждения) — с fgis.gost.ru

Ежедневно проверяет новые уведомления и обновляет дашборд.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# Загрузка переменных из .env файла (для локального запуска)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

from src.config import LAST_SEEN_PATH
from src.gost_config import GOST_LAST_SEEN_PATH
from src.scraper import (
    fetch_notifications_list,
    fetch_notification_detail,
    determine_stakeholders,
    _create_session,
)
from src.gost_scraper import fetch_gost_notifications
from src.polymer_filter import is_polymer_related, get_matched_keywords
from src.excel_writer import update_sp_excel, update_gost_excel
from src.dashboard_generator import update_registry, generate_dashboard, capture_dashboard_screenshot
from src.dashboard_config import DASHBOARD_REGISTRY_PATH, DATA_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _try_acquire_update_lock() -> object | None:
    lock_path = os.path.join(DATA_DIR, ".update.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f


def _release_update_lock(lock_file: object | None) -> None:
    if not lock_file:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


# ------------------------------------------------------------------
# Универсальные функции для работы с кэшем
# ------------------------------------------------------------------
def load_cache(path: str) -> set[str]:
    """Загружает ID ранее обработанных уведомлений из JSON-файла."""
    path = os.path.normpath(path)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Не удалось прочитать {path}: {e}")
        return set()


def save_cache(path: str, seen_ids: set[str]) -> None:
    """Сохраняет ID обработанных уведомлений в JSON-файл."""
    path = os.path.normpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(seen_ids)}, f, ensure_ascii=False, indent=2)
    logger.info(f"Сохранено {len(seen_ids)} ID в {path}")


def sync_caches_with_registry() -> None:
    """Синхронизирует кэши seen-ID с реестром дашборда.

    Все ID, которые уже есть в dashboard_registry.json, добавляются
    в gost_last_seen.json и last_seen.json, чтобы бэкфилл-записи
    не отправлялись повторно по email как «новые».
    """
    registry_path = os.path.normpath(DASHBOARD_REGISTRY_PATH)
    if not os.path.exists(registry_path):
        return

    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    # ГОСТ
    registry_gost_ids = {r["id"] for r in registry.get("gost", []) if r.get("id")}
    gost_seen = load_cache(GOST_LAST_SEEN_PATH)
    missing_gost = registry_gost_ids - gost_seen
    if missing_gost:
        logger.info(
            f"Синхронизация кэша ГОСТ: +{len(missing_gost)} ID из реестра "
            f"(было {len(gost_seen)}, стало {len(gost_seen | registry_gost_ids)})"
        )
        save_cache(GOST_LAST_SEEN_PATH, gost_seen | registry_gost_ids)

    # СП
    registry_sp_ids = {r["id"] for r in registry.get("sp", []) if r.get("id")}
    sp_seen = load_cache(LAST_SEEN_PATH)
    missing_sp = registry_sp_ids - sp_seen
    if missing_sp:
        logger.info(
            f"Синхронизация кэша СП: +{len(missing_sp)} ID из реестра "
            f"(было {len(sp_seen)}, стало {len(sp_seen | registry_sp_ids)})"
        )
        save_cache(LAST_SEEN_PATH, sp_seen | registry_sp_ids)


# Максимальный возраст уведомления для отправки по email (дней)
_EMAIL_MAX_AGE_DAYS = 14


def _is_recent(date_str: str, max_days: int = _EMAIL_MAX_AGE_DAYS) -> bool:
    """Проверяет, что дата не старше max_days дней от сегодня.

    Поддерживает форматы: DD.MM.YYYY, YYYY-MM-DD.
    Если дата не распознана — считаем «свежей» (на всякий случай отправим).
    """
    if not date_str or not date_str.strip():
        return True  # нет даты — лучше отправить, чем пропустить

    s = date_str.strip()
    dt = None

    # DD.MM.YYYY
    if "." in s:
        parts = s.split(".")
        if len(parts) == 3:
            try:
                dt = datetime.strptime(s, "%d.%m.%Y")
            except ValueError:
                pass

    # YYYY-MM-DD
    if dt is None and "-" in s:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            pass

    if dt is None:
        return True  # не удалось распарсить — лучше отправить

    cutoff = datetime.now() - timedelta(days=max_days)
    return dt >= cutoff


# ------------------------------------------------------------------
# Мониторинг СП (своды правил)
# ------------------------------------------------------------------
def run_sp_monitor(session) -> tuple[bool, list, list]:
    """Проверяет новые уведомления о сводах правил.

    Возвращает (ошибка, свежие_для_email, все_уведомления).
    Email НЕ отправляется — только сбор данных.
    """
    logger.info("--- Проверка уведомлений о сводах правил ---")

    seen_ids = load_cache(LAST_SEEN_PATH)
    logger.info(f"Ранее обработано уведомлений СП: {len(seen_ids)}")

    notifications = fetch_notifications_list(session)
    logger.info(f"Получено уведомлений со страницы: {len(notifications)}")

    if not notifications:
        logger.warning("Не удалось получить уведомления СП с сайта.")
        return True, [], []

    new_notifications = [
        n for n in notifications if n.get("id") and n["id"] not in seen_ids
    ]
    logger.info(f"Новых уведомлений СП: {len(new_notifications)}")

    if not new_notifications:
        logger.info("Новых уведомлений СП нет.")
        return False, [], notifications

    detailed_notifications = []
    for n in new_notifications:
        logger.info(f"Загрузка деталей: {n['title'][:60]}...")
        detail = fetch_notification_detail(n["url"], session)
        if detail:
            merged = {**n, **detail}
            merged["stakeholders"] = determine_stakeholders(merged)
            detailed_notifications.append(merged)
        else:
            n["stakeholders"] = determine_stakeholders(n)
            detailed_notifications.append(n)
        time.sleep(1)

    # Фильтр свежести: только уведомления не старше _EMAIL_MAX_AGE_DAYS дней
    fresh_notifications = [
        n for n in detailed_notifications
        if _is_recent(n.get("placement_date", n.get("date", "")))
    ]
    stale_count = len(detailed_notifications) - len(fresh_notifications)
    if stale_count:
        logger.info(
            f"Пропущено {stale_count} устаревших уведомлений СП "
            f"(старше {_EMAIL_MAX_AGE_DAYS} дней) — email не будет отправлен"
        )

    # Фильтрация полимерных СП и запись в Excel (все новые, не только свежие)
    polymer_sp = []
    for n in detailed_notifications:
        if is_polymer_related(n):
            n["matched_keywords"] = get_matched_keywords(n)
            polymer_sp.append(n)
    if polymer_sp:
        logger.info(f"Найдено {len(polymer_sp)} полимерных СП → Excel")
        update_sp_excel(polymer_sp)

    new_ids = {n["id"] for n in notifications if n.get("id")}
    save_cache(LAST_SEEN_PATH, seen_ids | new_ids)

    return False, fresh_notifications, detailed_notifications


# ------------------------------------------------------------------
# Мониторинг ГОСТов (публичные обсуждения)
# ------------------------------------------------------------------
def run_gost_monitor(session) -> tuple[bool, list, list]:
    """Проверяет новые уведомления о публичных обсуждениях ГОСТов.

    Возвращает (ошибка, свежие_для_email, все_уведомления).
    Email НЕ отправляется — только сбор данных.
    """
    logger.info("--- Проверка уведомлений о ГОСТах ---")

    seen_ids = load_cache(GOST_LAST_SEEN_PATH)
    logger.info(f"Ранее обработано уведомлений ГОСТ: {len(seen_ids)}")

    notifications = fetch_gost_notifications(session)

    if not notifications:
        logger.warning("Не удалось получить уведомления ГОСТ с ФГИС.")
        return True, [], []

    new_notifications = [
        n for n in notifications if n.get("id") and n["id"] not in seen_ids
    ]
    logger.info(f"Новых уведомлений ГОСТ: {len(new_notifications)}")

    if not new_notifications:
        logger.info("Новых уведомлений ГОСТ нет.")
        return False, [], notifications

    # Фильтр свежести: только уведомления не старше _EMAIL_MAX_AGE_DAYS дней
    fresh_notifications = [
        n for n in new_notifications
        if _is_recent(n.get("start_date", ""))
    ]
    stale_count = len(new_notifications) - len(fresh_notifications)
    if stale_count:
        logger.info(
            f"Пропущено {stale_count} устаревших уведомлений ГОСТ "
            f"(старше {_EMAIL_MAX_AGE_DAYS} дней) — email не будет отправлен"
        )

    # Фильтрация полимерных ГОСТов и запись в Excel (все новые, не только свежие)
    polymer_gost = []
    for n in new_notifications:
        if is_polymer_related(n):
            n["matched_keywords"] = get_matched_keywords(n)
            polymer_gost.append(n)
    if polymer_gost:
        logger.info(f"Найдено {len(polymer_gost)} полимерных ГОСТов → Excel")
        update_gost_excel(polymer_gost)

    # Сохраняем ВСЕ ID с последних страниц (не только новые)
    all_ids = {n["id"] for n in notifications if n.get("id")}
    save_cache(GOST_LAST_SEEN_PATH, seen_ids | all_ids)

    return False, fresh_notifications, notifications


# ------------------------------------------------------------------
# Главная функция
# ------------------------------------------------------------------
def main() -> int:
    lock_file = _try_acquire_update_lock()
    if not lock_file:
        print("RUN_LOCKED")
        logger.warning("Обновление уже выполняется в другом процессе — пропуск запуска.")
        return 0

    try:
        logger.info("=== Начало проверки уведомлений Росстандарта ===")

        # 0) Синхронизируем кэши с реестром дашборда (защита от повторных email)
        sync_caches_with_registry()

        session = _create_session()

        # 1) Сбор данных
        sp_error, sp_fresh, sp_all = run_sp_monitor(session)
        gost_error, gost_fresh, gost_all = run_gost_monitor(session)

        # 2) Обновляем реестр дашборда, генерируем HTML и делаем скриншот
        screenshot_path = None
        try:
            update_registry(gost_all, sp_all)
            generate_dashboard()
            screenshot_path = capture_dashboard_screenshot()
        except Exception as e:
            logger.error(f"Ошибка обновления дашборда: {e}")

        if sp_fresh or gost_fresh:
            logger.info(
                "Сбор завершен: свежие уведомления СП=%s, ГОСТ=%s",
                len(sp_fresh),
                len(gost_fresh),
            )
        else:
            logger.info("Новых уведомлений не найдено.")

        has_errors = sp_error or gost_error
        if not has_errors:
            logger.info("=== Проверка завершена успешно ===")
            return 0
        else:
            logger.error("=== Проверка завершена с ошибками ===")
            return 1
    finally:
        _release_update_lock(lock_file)


if __name__ == "__main__":
    sys.exit(main())
