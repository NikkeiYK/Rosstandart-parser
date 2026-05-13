from __future__ import annotations

import re
import html
import logging
import time
from datetime import datetime
from typing import Optional

import requests
import urllib3.util.connection

from src.gost_config import (
    GOST_API_URL,
    GOST_API_IPS,
    GOST_DETAIL_URL,
    GOST_STATUS_FILTER,
    GOST_PAGES_FROM_END,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# DNS-патч: fgis.gost.ru может не резолвиться через стандартный DNS,
# поэтому подставляем IP-адрес напрямую с автопереключением (failover).
# ------------------------------------------------------------------
_original_create_connection = urllib3.util.connection.create_connection
_active_ip_index = 0  # индекс текущего рабочего IP


def _patched_create_connection(address, *args, **kwargs):
    host, port = address
    if host == "fgis.gost.ru":
        host = GOST_API_IPS[_active_ip_index]
    return _original_create_connection((host, port), *args, **kwargs)


def _switch_to_next_ip() -> bool:
    """Переключается на следующий IP. Возвращает True, если есть ещё IP."""
    global _active_ip_index
    _active_ip_index += 1
    if _active_ip_index < len(GOST_API_IPS):
        logger.warning(
            f"Переключение на резервный IP: {GOST_API_IPS[_active_ip_index]}"
        )
        return True
    return False


urllib3.util.connection.create_connection = _patched_create_connection

# ------------------------------------------------------------------
# Общие параметры запроса (пустые фильтры обязательны, иначе 500)
# ------------------------------------------------------------------
_EMPTY_FILTERS = {
    "submittedPublicDiscussionDate": "",
    "submittedPublicDiscussionDateEnd": "",
    "publicDiscussionCompletedDate": "",
    "publicDiscussionCompletedDateEnd": "",
    "prns": "",
    "draftSt": "",
    "flUl": "",
    "tk": "",
    "programSubsection": "",
    "documentType": "",
}


def _extract_uuid(prns_html: str) -> Optional[str]:
    """Извлекает UUID из HTML-ссылки в поле @rsprsPrns:prns.

    Пример: <a target='_blank' href='../rsprs/nds-details?uuid=6e7717ef-...'>1.13.465-1.164.19</a>
    """
    match = re.search(r"uuid=([a-f0-9\-]{36})", prns_html)
    return match.group(1) if match else None


def _extract_prns_code(prns_html: str) -> str:
    """Извлекает шифр ПНС из HTML-ссылки."""
    match = re.search(r">([^<]+)<", prns_html)
    return match.group(1).strip() if match else ""


def fetch_gost_notifications(
    session: Optional[requests.Session] = None,
    full_backfill: bool = False,
) -> list:
    """Получает уведомления о ГОСТах.

    Args:
        session: HTTP-сессия
        full_backfill: Если True — загружает ВСЕ записи за 2026 год
                       по всем статусам (как backfill_2026.py).
                       Если False — загружает последние N страниц
                       только со статусом "Вынесен на публичное обсуждение".

    Returns:
        Список уведомлений
    """
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })

    if full_backfill:
        return _fetch_gost_full_backfill(session)

    # 1) Узнаём total pages — с автопереключением IP при ошибке
    global _active_ip_index
    _active_ip_index = 0  # сброс на начало при каждом запуске

    data = None
    while True:
        try:
            resp = session.get(
                GOST_API_URL,
                params={
                    **_EMPTY_FILTERS,
                    "statusDocumentNDS": GOST_STATUS_FILTER,
                    "page": 1,
                    "rows": 20,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Ошибка запроса API ФГИС ({GOST_API_IPS[_active_ip_index]}): {e}")
            if not _switch_to_next_ip():
                logger.error("Все IP-адреса ФГИС недоступны.")
                return []

    if data is None:
        return []

    total_pages_raw = data.get("total", "0")
    # API возвращает "2 741" с пробелом-разделителем
    total_pages = int(str(total_pages_raw).replace(" ", "").replace("\xa0", ""))
    total_records = data.get("records", "0")
    logger.info(
        f"ГОСТ: всего {total_records} записей на {total_pages} страницах "
        f"(статус: {GOST_STATUS_FILTER})"
    )

    if total_pages == 0:
        return []

    # 2) Загружаем последние N страниц (самые свежие)
    all_notifications = []
    # Первая страница уже загружена — если это последняя, используем её
    pages_to_fetch = []
    for i in range(GOST_PAGES_FROM_END):
        page_num = total_pages - i
        if page_num >= 1:
            pages_to_fetch.append(page_num)

    # Первая страница (page 1) уже загружена, если total_pages <= GOST_PAGES_FROM_END
    # Но для упрощения — загружаем нужные страницы заново
    for page_num in sorted(pages_to_fetch):
        logger.info(f"ГОСТ: загрузка страницы {page_num}/{total_pages}...")
        try:
            resp = session.get(
                GOST_API_URL,
                params={
                    **_EMPTY_FILTERS,
                    "statusDocumentNDS": GOST_STATUS_FILTER,
                    "page": page_num,
                    "rows": 20,
                },
                timeout=30,
            )
            resp.raise_for_status()
            page_data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Ошибка загрузки страницы {page_num}: {e}")
            continue

        rows = page_data.get("rows", [])
        for row in rows:
            notification = _parse_api_row(row)
            if notification:
                all_notifications.append(notification)

    logger.info(f"ГОСТ: получено {len(all_notifications)} уведомлений с последних страниц")
    return all_notifications


def _fetch_gost_full_backfill(session: requests.Session) -> list:
    """Загружает ВСЕ ГОСТ-уведомления за 2026 год по всем статусам.

    Аналогично backfill_2026.py, но возвращает список вместо сохранения в реестр.
    """
    from src.dashboard_config import ALL_GOST_STATUSES

    all_records = []
    today = datetime.now().strftime("%Y-%m-%d")
    date_from = "2026-01-01"
    batch_size = 20
    request_delay = 0.5

    for status in ALL_GOST_STATUSES:
        logger.info(f"--- Статус: {status} ---")

        # Узнаём количество страниц
        params = {
            **_EMPTY_FILTERS,
            "submittedPublicDiscussionDate": date_from,
            "statusDocumentNDS": status,
            "page": 1,
            "rows": batch_size,
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

            time.sleep(request_delay)
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

    logger.info(f"Всего загружено (full backfill): {len(all_records)} записей")
    return all_records


def _parse_api_row(row: dict) -> Optional[dict]:
    """Преобразует строку JSON API в словарь уведомления."""
    prns_html = row.get("@rsprsPrns:prns", "")
    uuid = _extract_uuid(prns_html)
    if not uuid:
        return None

    prns_code = _extract_prns_code(prns_html)
    detail_url = GOST_DETAIL_URL.format(uuid=uuid)

    return {
        "id": uuid,
        "prns_code": prns_code,
        "program": html.unescape(row.get("@rsprs-nds:subProgram", "")),
        "doc_type": html.unescape(row.get("@rsprs-nds:gostR", "")),
        "project_name": html.unescape(row.get("@rsprsPrns:draftSt", "")),
        "technical_committee": html.unescape(row.get("@rsprsPrns:tk", "")),
        "developer": html.unescape(row.get("@rsprsDeveloper:flUl", "")),
        "start_date": row.get("@rsprs-nds:submitted-public-discussion-date", ""),
        "end_date": row.get("@rsprs-nds:public-discussion-completed-date", ""),
        "status": row.get("@lecm-statemachine:status", ""),
        "url": detail_url,
    }
