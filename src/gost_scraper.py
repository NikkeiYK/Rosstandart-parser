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


def _request_json(
    session: requests.Session,
    params: dict,
    *,
    timeout: int = 30,
) -> Optional[dict]:
    global _active_ip_index
    while True:
        try:
            resp = session.get(GOST_API_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            ip = GOST_API_IPS[_active_ip_index] if _active_ip_index < len(GOST_API_IPS) else ""
            logger.error(f"Ошибка запроса API ФГИС ({ip}): {e}")
            if not _switch_to_next_ip():
                logger.error("Все IP-адреса ФГИС недоступны.")
                return None



def _extract_uuid(prns_html: str) -> Optional[str]:
    """Извлекает UUID из HTML-ссылки в поле @rsprsPrns:prns.

    """
    match = re.search(r"uuid=([a-f0-9\-]{36})", prns_html)
    return match.group(1) if match else None


def _extract_prns_code(prns_html: str) -> str:
    """Извлекает шифр ПНС из HTML-ссылки."""
    match = re.search(r">([^<]+)<", prns_html)
    return match.group(1).strip() if match else ""


def fetch_gost_notifications(
    session: Optional[requests.Session] = None,
    *,
    status: str = GOST_STATUS_FILTER,
    pages_from_end: int = GOST_PAGES_FROM_END,
    date_from: Optional[str] = None,
) -> list:
    """Получает последние уведомления о ГОСТах для заданного статуса.

    Стратегия: сначала узнаём общее количество страниц,
    затем загружаем последние N страниц (самые свежие записи).
    """
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })

    # 1) Узнаём total pages — с автопереключением IP при ошибке
    global _active_ip_index
    _active_ip_index = 0  # сброс на начало при каждом запуске

    params = {
        **_EMPTY_FILTERS,
        "statusDocumentNDS": status,
        "page": 1,
        "rows": 20,
    }
    if date_from:
        params["submittedPublicDiscussionDate"] = date_from

    data = _request_json(session, params)
    if not data:
        return []

    total_pages_raw = data.get("total", "0")
    # API возвращает "2 741" с пробелом-разделителем
    total_pages = int(str(total_pages_raw).replace(" ", "").replace("\xa0", ""))
    total_records = data.get("records", "0")
    logger.info(
        f"ГОСТ: всего {total_records} записей на {total_pages} страницах "
        f"(статус: {status})"
    )

    if total_pages == 0:
        return []

    # 2) Загружаем последние N страниц (самые свежие)
    all_notifications = []
    # Первая страница уже загружена — если это последняя, используем её
    pages_to_fetch = []
    for i in range(pages_from_end):
        page_num = total_pages - i
        if page_num >= 1:
            pages_to_fetch.append(page_num)

    # Первая страница (page 1) уже загружена, если total_pages <= GOST_PAGES_FROM_END
    # Но для упрощения — загружаем нужные страницы заново
    for page_num in sorted(pages_to_fetch):
        logger.info(f"ГОСТ: загрузка страницы {page_num}/{total_pages}...")
        params["page"] = page_num
        page_data = _request_json(session, params)
        if not page_data:
            continue

        rows = page_data.get("rows", [])
        for row in rows:
            notification = _parse_api_row(row)
            if notification:
                all_notifications.append(notification)

    logger.info(f"ГОСТ: получено {len(all_notifications)} уведомлений с последних страниц")
    return all_notifications


def fetch_gost_notifications_multi_status(
    statuses: list[str],
    session: Optional[requests.Session] = None,
    *,
    pages_from_end: int = GOST_PAGES_FROM_END,
    date_from: Optional[str] = None,
) -> list[dict]:
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })

    combined: dict[str, dict] = {}
    for status in statuses:
        items = fetch_gost_notifications(
            session,
            status=status,
            pages_from_end=pages_from_end,
            date_from=date_from,
        )
        for item in items:
            gid = item.get("id")
            if gid:
                combined[gid] = item
    return list(combined.values())


def backfill_gost_notifications(
    session: requests.Session,
    *,
    statuses: list[str],
    date_from: str,
    batch_size: int = 20,
    request_delay_seconds: float = 0.3,
) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    all_records: list[dict] = []

    for status in statuses:
        global _active_ip_index
        _active_ip_index = 0

        params = {
            **_EMPTY_FILTERS,
            "submittedPublicDiscussionDate": date_from,
            "statusDocumentNDS": status,
            "page": 1,
            "rows": batch_size,
        }
        data = _request_json(session, params)
        if not data:
            continue

        total_pages_raw = data.get("total", "0")
        total_pages = int(str(total_pages_raw).replace(" ", "").replace("\xa0", ""))
        total_records = data.get("records", "0")
        logger.info(f"ГОСТ backfill: статус='{status}', записей={total_records}, страниц={total_pages}")

        for row in data.get("rows", []):
            notification = _parse_api_row(row)
            if notification:
                notification["fetched_date"] = today
                notification["source"] = "gost"
                all_records.append(notification)

        for page_num in range(2, total_pages + 1):
            time.sleep(request_delay_seconds)
            params["page"] = page_num
            page_data = _request_json(session, params)
            if not page_data:
                continue
            for row in page_data.get("rows", []):
                notification = _parse_api_row(row)
                if notification:
                    notification["fetched_date"] = today
                    notification["source"] = "gost"
                    all_records.append(notification)

    logger.info(f"ГОСТ backfill: всего загружено {len(all_records)} записей")
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
