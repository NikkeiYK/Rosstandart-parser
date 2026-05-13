from __future__ import annotations

import re
import json
import time
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from src.config import (
    BASE_URL,
    COMPONENT_ID,
    PAGES_TO_CHECK,
    DEVELOPER_STAKEHOLDERS,
    KEYWORD_STAKEHOLDERS,
)

logger = logging.getLogger(__name__)

SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

SITE_ORIGIN = "https://www.rst.gov.ru"


def _create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    return session


def _extract_notification_id(url: str) -> Optional[str]:
    """Извлекает ID уведомления из navigationalstate в URL."""
    match = re.search(r"navigationalstate=([^&]+)", url)
    if not match:
        return None
    nav_state = match.group(1)
    # ID закодирован в Base64 Java serialization, ищем паттерн id в decoded строке
    # Формат: ...id\x00\x00\x00\x01\x00\x06 63842...
    try:
        import base64
        decoded = base64.b64decode(nav_state.replace("JBPNS_", "")).decode(
            "latin-1"
        )
        id_match = re.search(r"id.{1,10}?(\d{3,})", decoded)
        if id_match:
            return id_match.group(1).strip()
    except Exception:
        pass
    # Фоллбэк: ищем ID прямо в URL-encoded строке
    id_match = re.search(r"(%20|%C2%A0|\s)(\d{3,})", nav_state)
    if id_match:
        return id_match.group(2)
    return nav_state[:20]  # уникальный ключ как фоллбэк


def fetch_notifications_list(session: Optional[requests.Session] = None) -> list:
    """Получает список уведомлений с первых N страниц."""
    if session is None:
        session = _create_session()

    all_notifications = []

    for page_num in range(PAGES_TO_CHECK):
        logger.info(f"Загрузка страницы {page_num + 1}...")
        try:
            if page_num == 0:
                resp = session.get(BASE_URL, timeout=30)
            else:
                # Пагинация через interactionstate
                resp = session.get(
                    BASE_URL,
                    params={
                        "portal:isSecure": "true",
                        "portal:componentId": COMPONENT_ID,
                        "interactionstate": _build_page_state(page_num),
                        "portal:type": "action",
                    },
                    timeout=30,
                )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Ошибка загрузки страницы {page_num + 1}: {e}")
            break

        notifications = _parse_list_page(resp.text)
        if not notifications:
            logger.info(f"Страница {page_num + 1} пуста, остановка.")
            break
        all_notifications.extend(notifications)
        time.sleep(1)  # вежливая пауза

    return all_notifications


def _build_page_state(page_num: int) -> str:
    """Строит interactionstate для пагинации (упрощённый вариант)."""
    import base64
    # Формат Java serialized state для пагинации
    # page=0 — первая страница, page=1 — вторая и т.д.
    state_data = (
        f"\x00\x06length\x00\x00\x00\x01\x00\x0210"
        f"\x00\x04page\x00\x00\x00\x01\x00\x01{page_num}"
        f"\x00\x05state\x00\x00\x00\x01\x00\x06ACTUAL"
        f"\x00\x07__EOF__"
    )
    encoded = base64.b64encode(state_data.encode("latin-1")).decode()
    return f"JBPNS_{encoded}"


def _parse_list_page(html: str) -> list:
    """Парсит HTML списка уведомлений."""
    soup = BeautifulSoup(html, "html.parser")
    notifications = []

    # Ищем ссылки на уведомления по характерному паттерну URL
    links = soup.find_all("a", href=re.compile(r"navigationalstate=.*notification"))
    if not links:
        # Фоллбэк: ищем все ссылки с navigationalstate
        links = soup.find_all("a", href=re.compile(r"navigationalstate="))

    # Фразы, которые не являются уведомлениями
    skip_phrases = [
        "версия для слабовидящих", "назад", "вперед", "архив",
        "сбросить фильтр", "поиск", "войти", "регистрация",
    ]

    for link in links:
        href = link.get("href", "")
        title = link.get_text(strip=True)
        if not title or len(title) < 15:
            continue

        # Пропускаем навигационные ссылки
        if any(phrase in title.lower() for phrase in skip_phrases):
            continue

        notification_id = _extract_notification_id(href)
        # Пропускаем ссылки без валидного числового ID
        if not notification_id or not notification_id.isdigit():
            continue
        full_url = urljoin(SITE_ORIGIN, href) if not href.startswith("http") else href

        # Дата обычно в соседнем элементе (td или span)
        date_text = ""
        parent = link.find_parent(["tr", "div", "li"])
        if parent:
            date_match = re.search(r"\d{2}\.\d{2}\.\d{2,4}", parent.get_text())
            if date_match:
                date_text = date_match.group(0)

        notifications.append({
            "id": notification_id,
            "title": title,
            "url": full_url,
            "date": date_text,
        })

    return notifications


def fetch_notification_detail(
    url: str, session: Optional[requests.Session] = None
) -> dict:
    """Получает детали конкретного уведомления."""
    if session is None:
        session = _create_session()

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Ошибка загрузки деталей: {e}")
        return {}

    return _parse_detail_page(resp.text, url)


def _parse_detail_page(html: str, url: str) -> dict:
    """Парсит страницу детали уведомления.

    Данные могут быть в JavaScript-объекте notifications.selected или в HTML.
    """
    detail = {
        "notification_type": "",
        "doc_type": "",
        "project_name": "",
        "developer": "",
        "placement_date": "",
        "attachments": [],
        "url": url,
    }

    # Попытка 1: извлечь из JavaScript объекта
    js_data = _extract_js_notification(html)
    if js_data:
        detail["notification_type"] = js_data.get("typeName", "")
        detail["doc_type"] = js_data.get("docTypeName", "")
        detail["project_name"] = js_data.get("title", "")
        detail["developer"] = js_data.get("developer", "")
        detail["placement_date"] = js_data.get("date", "")
        detail["attachments"] = js_data.get("attachments", [])

    # Попытка 2: парсинг HTML напрямую
    soup = BeautifulSoup(html, "html.parser")

    if not detail["notification_type"]:
        # Ищем тип уведомления по характерным фразам
        text = soup.get_text()
        if "завершении публичного обсуждения" in text:
            detail["notification_type"] = "о завершении публичного обсуждения проекта"
        elif "разработке проекта" in text:
            detail["notification_type"] = "о разработке проекта"

    if not detail["developer"]:
        # Ищем разработчика — обычно Министерство/ведомство
        text = soup.get_text()
        dev_match = re.search(
            r"(?:разработчик[аи]?\s*:?\s*)(Министерство[^.;,\n]{10,200}|"
            r"Федеральн[а-я]+\s+служб[а-я]+[^.;,\n]{10,200}|"
            r"Госкорпорац[а-я]+[^.;,\n]{10,100})",
            text,
            re.IGNORECASE,
        )
        if dev_match:
            detail["developer"] = dev_match.group(1).strip()

    if not detail["project_name"]:
        # Ищем название проекта по кавычкам в тексте,
        # но только в основном контенте (пропускаем меню)
        text = soup.get_text()
        # Ищем все совпадения в кавычках и берём самое длинное — это проект
        name_matches = re.findall(r"«([^»]{20,})»", text)
        if name_matches:
            detail["project_name"] = max(name_matches, key=len).strip()

    if not detail["placement_date"]:
        text = soup.get_text()
        date_match = re.search(r"\d{2}\.\d{2}\.\d{2,4}", text)
        if date_match:
            detail["placement_date"] = date_match.group(0)

    if not detail["attachments"]:
        # Ищем ссылки на файлы
        file_links = soup.find_all("a", href=re.compile(r"file-service|\.pdf|\.doc"))
        for fl in file_links:
            detail["attachments"].append({
                "name": fl.get_text(strip=True),
                "url": urljoin(SITE_ORIGIN, fl.get("href", "")),
            })

    return detail


def _extract_js_notification(html: str) -> Optional[dict]:
    """Извлекает данные из JavaScript-объекта notifications.selected."""
    # Ищем JSON-подобный объект в JavaScript
    match = re.search(
        r"notifications\.selected\s*=\s*(\{.+?\});", html, re.DOTALL
    )
    if not match:
        return None

    js_text = match.group(1)
    # Попытка парсинга как JSON (может потребоваться очистка)
    try:
        # Заменяем одинарные кавычки на двойные для JSON
        json_text = js_text.replace("'", '"')
        # Убираем trailing commas
        json_text = re.sub(r",\s*}", "}", json_text)
        json_text = re.sub(r",\s*]", "]", json_text)
        data = json.loads(json_text)
        return data
    except json.JSONDecodeError:
        pass

    # Фоллбэк: regex-извлечение полей
    result = {}
    for field in ["typeName", "docTypeName", "id"]:
        m = re.search(rf"{field}\s*:\s*['\"]([^'\"]+)['\"]", js_text)
        if m:
            result[field] = m.group(1)

    # Извлекаем attachments
    attachments_match = re.search(r"attachments\s*:\s*\[(.+?)\]", js_text, re.DOTALL)
    if attachments_match:
        att_text = attachments_match.group(1)
        att_items = re.findall(
            r"name\s*:\s*['\"]([^'\"]+)['\"].*?uuid\s*:\s*['\"]([^'\"]+)['\"]",
            att_text,
            re.DOTALL,
        )
        result["attachments"] = [
            {"name": name, "url": f"{SITE_ORIGIN}/file-service/file/load/{uuid}"}
            for name, uuid in att_items
        ]

    return result if result else None


def determine_stakeholders(detail: dict) -> list:
    """Определяет заинтересованных лиц на основе разработчика и ключевых слов."""
    stakeholders = set()
    developer = detail.get("developer", "").lower()
    project_name = detail.get("project_name", "").lower()
    title = detail.get("title", "").lower()
    combined_text = f"{project_name} {title}"

    # По разработчику
    for key, persons in DEVELOPER_STAKEHOLDERS.items():
        if key.lower() in developer:
            stakeholders.update(persons)

    # По ключевым словам
    for keyword, persons in KEYWORD_STAKEHOLDERS.items():
        if keyword in combined_text:
            stakeholders.update(persons)

    # Если ничего не нашли — общие заинтересованные лица
    if not stakeholders:
        stakeholders.add("Проектные и строительные организации")
        stakeholders.add("Органы технического регулирования")

    return sorted(stakeholders)
