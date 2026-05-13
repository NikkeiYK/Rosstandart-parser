import os

from src.dashboard_config import DATA_DIR

# JSON API ФГИС Росстандарта — уведомления о ГОСТах
# Данные загружаются через API fgis.gost.ru (iframe на странице rst.gov.ru)
GOST_API_URL = "https://fgis.gost.ru/share/proxy/alfresco-noauth/rsprs/public/nds"

# IP-адреса fgis.gost.ru (DNS может не резолвиться, используем прямое подключение)
GOST_API_IPS = ["212.164.138.14", "212.164.138.19"]

# Страница на rst.gov.ru (для ссылки в письме)
GOST_PAGE_URL = "https://www.rst.gov.ru/portal/gost/home/activity/standardization/notification/stand_doc_notifications"

# Детальная страница одной записи
GOST_DETAIL_URL = "https://fgis.gost.ru/share/page/rsprs/nds-details?uuid={uuid}"

# Фильтр по статусу — «Вынесен на публичное обсуждение»
GOST_STATUS_FILTER = "Вынесен на публичное обсуждение"

# Файл для хранения уже обработанных уведомлений о ГОСТах
GOST_LAST_SEEN_PATH = os.path.join(DATA_DIR, "gost_last_seen.json")

# Сколько последних страниц проверять (каждая по 20 записей)
GOST_PAGES_FROM_END = 2
