"""Конфигурация дашборда мониторинга Росстандарта."""

import os

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _resolve_data_dir() -> str:
    """Локально — каталог `data/` рядом с проектом.

    В Amvera при `persistenceMount: /data` задайте `DATA_DIR=/data` в переменных приложения
    или положитесь на авто-режим: при `AMVERA=1` и существующей папке `/data` она
    используется автоматически.
    """
    raw = os.environ.get("DATA_DIR", "").strip()
    if raw:
        return os.path.normpath(raw)
    if os.environ.get("AMVERA") == "1" and os.path.isdir("/data"):
        return "/data"
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))


DATA_DIR = _resolve_data_dir()

# Единый реестр всех уведомлений 2026 года
DASHBOARD_REGISTRY_PATH = os.path.join(DATA_DIR, "dashboard_registry.json")

# Конфигурация «наших» технических комитетов
TK_CONFIG_PATH = os.path.join(DATA_DIR, "my_technical_committees.json")

# HTML дашборда (в том же DATA_DIR, чтобы Web и Cron видели один файл при общем томе)
DASHBOARD_OUTPUT_PATH = os.path.join(DATA_DIR, "dashboard.html")

# Начало периода для загрузки данных
YEAR_START = "01.01.2026"

# Все статусы ГОСТ для полной загрузки
ALL_GOST_STATUSES = [
    "Вынесен на публичное обсуждение",
    "Направлено уведомление о завершении публичного обсуждения",
    "Продлен срок публичного обсуждения",
    "На доработке",
    "Публичное обсуждение завершено",
]
