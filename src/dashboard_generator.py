"""Генератор интерактивного HTML-дашборда мониторинга Росстандарта.

Читает data/dashboard_registry.json + data/my_technical_committees.json
и генерирует dashboard.html с таблицами, фильтрами и графиками.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections import Counter
from datetime import datetime

from src.dashboard_config import (
    DATA_DIR,
    DASHBOARD_REGISTRY_PATH,
    TK_CONFIG_PATH,
    DASHBOARD_OUTPUT_PATH,
)
from src.excel_writer import read_polymer_gost_stats, _migrate_gost_add_comment_column, GOST_EXCEL_PATH

logger = logging.getLogger(__name__)


def ensure_tk_config_exists() -> None:
    if os.path.exists(TK_CONFIG_PATH):
        return
    fallback = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "data", "my_technical_committees.json")
    )
    if not os.path.exists(fallback):
        return
    os.makedirs(os.path.dirname(TK_CONFIG_PATH), exist_ok=True)
    try:
        shutil.copyfile(fallback, TK_CONFIG_PATH)
        logger.info(f"Скопирован конфиг ТК: {TK_CONFIG_PATH}")
    except OSError:
        return


# ------------------------------------------------------------------
# Работа с реестром
# ------------------------------------------------------------------
def _load_json(path: str) -> dict:
    """Загружает JSON-файл."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: dict) -> None:
    """Сохраняет JSON-файл."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_registry(
    gost_notifications: list[dict],
    sp_notifications: list[dict],
    *,
    backfill: bool = False,
) -> None:
    registry = _load_json(DASHBOARD_REGISTRY_PATH)
    if not registry:
        registry = {
            "metadata": {
                "last_updated": "",
                "gost_count": 0,
                "sp_count": 0,
                "last_backfill": "",
            },
            "gost": [],
            "sp": [],
        }

    today = datetime.now().strftime("%Y-%m-%d")

    # ГОСТы
    existing_gost_by_id = {
        r["id"]: r for r in registry.get("gost", []) if r.get("id")
    }
    new_gost = 0
    updated_gost = 0
    for n in gost_notifications:
        nid = n.get("id")
        if not nid:
            continue
        if nid not in existing_gost_by_id:
            entry = {**n, "fetched_date": today, "source": "gost"}
            registry["gost"].append(entry)
            existing_gost_by_id[nid] = entry
            new_gost += 1
            continue

        existing = existing_gost_by_id[nid]
        changed = False
        for k in (
            "status",
            "end_date",
            "technical_committee",
            "developer",
            "doc_type",
            "project_name",
            "program",
            "prns_code",
            "start_date",
            "url",
        ):
            if k not in n:
                continue
            val = n.get(k)
            if val is None or val == "":
                continue
            if existing.get(k) != val:
                existing[k] = val
                changed = True
        if changed:
            updated_gost += 1

    # СП
    existing_sp_ids = {r["id"] for r in registry.get("sp", [])}
    new_sp = 0
    for n in sp_notifications:
        nid = n.get("id")
        if nid and nid not in existing_sp_ids:
            entry = {**n, "fetched_date": today, "source": "sp"}
            registry["sp"].append(entry)
            existing_sp_ids.add(nid)
            new_sp += 1

    # Метаданные
    now = datetime.now().isoformat()
    registry["metadata"]["last_updated"] = now
    registry["metadata"]["gost_count"] = len(registry["gost"])
    registry["metadata"]["sp_count"] = len(registry["sp"])
    if backfill:
        registry["metadata"]["last_backfill"] = now

    _save_json(DASHBOARD_REGISTRY_PATH, registry)

    if new_gost or updated_gost or new_sp:
        logger.info(
            f"Реестр дашборда: +{new_gost} ГОСТов, ~{updated_gost} обновлено, +{new_sp} СП "
            f"(всего: {registry['metadata']['gost_count']} ГОСТов, "
            f"{registry['metadata']['sp_count']} СП)"
        )


# ------------------------------------------------------------------
# Статистика для графиков
# ------------------------------------------------------------------
def _compute_stats(registry: dict) -> dict:
    """Вычисляет агрегированную статистику для графиков."""
    gost_list = registry.get("gost", [])
    sp_list = registry.get("sp", [])

    # Статусы ГОСТ
    status_counter = Counter(r.get("status", "Неизвестно") for r in gost_list)

    # По месяцам (дата начала обсуждения)
    month_counter = Counter()
    for r in gost_list:
        date_str = r.get("start_date", "")
        if date_str and len(date_str) >= 10:
            # Формат: DD.MM.YYYY
            parts = date_str.split(".")
            if len(parts) == 3:
                month_key = f"{parts[2]}-{parts[1]}"  # YYYY-MM
                month_counter[month_key] += 1

    # Сортируем месяцы
    sorted_months = sorted(month_counter.keys())
    month_labels = []
    month_values = []
    month_names_ru = {
        "01": "Янв", "02": "Фев", "03": "Мар", "04": "Апр",
        "05": "Май", "06": "Июн", "07": "Июл", "08": "Авг",
        "09": "Сен", "10": "Окт", "11": "Ноя", "12": "Дек",
    }
    for m in sorted_months:
        parts = m.split("-")
        if len(parts) == 2:
            month_labels.append(f"{month_names_ru.get(parts[1], parts[1])} {parts[0]}")
            month_values.append(month_counter[m])

    # Все ТК (сортировка по убыванию количества)
    tk_counter = Counter()
    for r in gost_list:
        tk = r.get("technical_committee", "").strip()
        if tk:
            tk_counter[tk] += 1
    all_tks = tk_counter.most_common()

    # Типы документов
    doc_type_counter = Counter(r.get("doc_type", "Не указан") for r in gost_list)

    # Количество активных
    active_count = sum(
        1 for r in gost_list
        if r.get("status") == "Вынесен на публичное обсуждение"
    )

    return {
        "total_gost": len(gost_list),
        "total_sp": len(sp_list),
        "active_count": active_count,
        "completed_count": len(gost_list) - active_count,
        "status_labels": list(status_counter.keys()),
        "status_values": list(status_counter.values()),
        "month_labels": month_labels,
        "month_values": month_values,
        "all_tk_labels": [t[0][:50] for t in all_tks],
        "all_tk_values": [t[1] for t in all_tks],
        "doc_type_labels": list(doc_type_counter.keys()),
        "doc_type_values": list(doc_type_counter.values()),
    }


# ------------------------------------------------------------------
# Генерация HTML
# ------------------------------------------------------------------
def _is_2026(date_str: str) -> bool:
    """Проверяет, относится ли дата к 2026 году.

    Поддерживает форматы: DD.MM.YYYY, DD.MM.YY, YYYY-MM-DD.
    """
    if not date_str:
        return False
    s = date_str.strip()
    # DD.MM.YYYY
    if s.endswith(".2026"):
        return True
    # DD.MM.YY
    if s.endswith(".26") and len(s) == 8:
        return True
    # YYYY-MM-DD
    if s.startswith("2026-"):
        return True
    return False


def generate_dashboard() -> None:
    """Генерирует HTML-дашборд из реестра (только данные 2026 года)."""
    registry = _load_json(DASHBOARD_REGISTRY_PATH)
    if not registry:
        logger.warning("Реестр пуст — дашборд не сгенерирован.")
        return

    # Фильтруем: только 2026 год
    gost_2026 = [
        r for r in registry.get("gost", [])
        if _is_2026(r.get("start_date", ""))
        or _is_2026(r.get("fetched_date", ""))
    ]
    sp_2026 = [
        r for r in registry.get("sp", [])
        if _is_2026(r.get("placement_date", ""))
    ]

    filtered_registry = {
        "metadata": registry.get("metadata", {}),
        "gost": gost_2026,
        "sp": sp_2026,
    }

    ensure_tk_config_exists()
    tk_config = _load_json(TK_CONFIG_PATH)
    my_tks = tk_config.get("committees", [])
    stats = _compute_stats(filtered_registry)

    # Полимерная статистика из Excel
    _migrate_gost_add_comment_column(GOST_EXCEL_PATH)
    polymer_stats = read_polymer_gost_stats()

    last_updated = registry.get("metadata", {}).get("last_updated", "")
    if last_updated:
        try:
            dt = datetime.fromisoformat(last_updated)
            last_updated = dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass

    # Готовим JSON-данные для встраивания в HTML
    dashboard_data = {
        "gost": gost_2026,
        "sp": sp_2026,
        "myTechnicalCommittees": my_tks,
        "stats": stats,
        "lastUpdated": last_updated,
        "polymerStats": polymer_stats,
    }
    data_json = json.dumps(dashboard_data, ensure_ascii=False)

    html = _build_html(data_json, stats, last_updated, my_tks, polymer_stats)

    with open(DASHBOARD_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(
        f"Дашборд сгенерирован: {DASHBOARD_OUTPUT_PATH} "
        f"({stats['total_gost']} ГОСТов, {stats['total_sp']} СП)"
    )


# ------------------------------------------------------------------
# Скриншот дашборда (Chrome headless)
# ------------------------------------------------------------------
_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_SCREENSHOT_PATH = os.path.normpath(os.path.join(DATA_DIR, "dashboard_screenshot.png"))


def capture_dashboard_screenshot() -> str | None:
    """Делает скриншот dashboard.html через Chrome headless.

    Returns:
        Путь к PNG-файлу или None при ошибке.
    """
    if not os.path.exists(_CHROME_PATH):
        logger.warning(f"Chrome не найден: {_CHROME_PATH} — скриншот пропущен.")
        return None

    dashboard_abs = os.path.abspath(DASHBOARD_OUTPUT_PATH)
    if not os.path.exists(dashboard_abs):
        logger.warning(f"Дашборд не найден: {dashboard_abs} — скриншот пропущен.")
        return None

    file_url = f"file://{dashboard_abs}"

    cmd = [
        _CHROME_PATH,
        "--headless",
        f"--screenshot={_SCREENSHOT_PATH}",
        "--window-size=1400,900",
        "--disable-gpu",
        "--no-sandbox",
        "--virtual-time-budget=5000",
        "--hide-scrollbars",
        file_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if os.path.exists(_SCREENSHOT_PATH):
            size_kb = os.path.getsize(_SCREENSHOT_PATH) // 1024
            logger.info(f"Скриншот дашборда: {_SCREENSHOT_PATH} ({size_kb} КБ)")
            return _SCREENSHOT_PATH
        else:
            logger.error(f"Chrome завершился, но скриншот не создан. stderr: {result.stderr[:300]}")
            return None
    except subprocess.TimeoutExpired:
        logger.error("Chrome headless: таймаут (30 сек)")
        return None
    except OSError as e:
        logger.error(f"Ошибка запуска Chrome: {e}")
        return None


def _build_html(
    data_json: str,
    stats: dict,
    last_updated: str,
    my_tks: list[str],
    polymer_stats: dict | None = None,
) -> str:
    """Формирует полный HTML-документ дашборда."""
    status_colors = (
        "['#008B92','#FC5A41','#e67e22','#8e44ad','#01313D','#95a5a6']"
    )
    stats_json = json.dumps(stats, ensure_ascii=False)
    my_tks_json = json.dumps(my_tks, ensure_ascii=False)
    my_tks_display = ", ".join(my_tks) if my_tks else "Не настроено"
    if polymer_stats is None:
        polymer_stats = {"total": 0, "commented": 0}
    polymer_stats_json = json.dumps(polymer_stats, ensure_ascii=False)
    # Высота графика ТК: минимум 400px, 28px на каждый ТК
    tk_count = len(stats.get("all_tk_labels", []))
    chart_height = max(400, tk_count * 28)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Мониторинг Росстандарта 2026</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.datatables.net/1.13.8/css/dataTables.bootstrap5.min.css" rel="stylesheet">
<link href="https://cdn.datatables.net/buttons/2.4.2/css/buttons.bootstrap5.min.css" rel="stylesheet">
<style>
  body {{ background: #f4f6f8; font-family: 'Segoe UI', Arial, sans-serif; }}
  .stat-card {{ background: #fff; border-radius: 12px; padding: 20px; text-align: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); transition: transform 0.2s;
    border-top: 3px solid #008B92; }}
  .stat-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 16px rgba(0,0,0,0.12); }}
  .stat-number {{ font-size: 2.2rem; font-weight: 700; }}
  .stat-label {{ color: #556; font-size: 0.85rem; margin-top: 4px; }}
  .chart-container {{ background: #fff; border-radius: 12px; padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 20px; }}
  .tk-highlight {{ background-color: #e6f7f8 !important; border-left: 3px solid #008B92; }}
  .badge-active {{ background: #008B92; }}
  .badge-completed {{ background: #95a5a6; }}
  .badge-extended {{ background: #e67e22; }}
  .badge-revision {{ background: #8e44ad; }}
  .badge-notification {{ background: #01313D; }}
  .nav-tabs .nav-link {{ font-weight: 500; color: #555; }}
  .nav-tabs .nav-link.active {{ color: #01313D; border-color: #008B92 #008B92 #fff; font-weight: 600; }}
  .tab-content {{ background: #fff; border-radius: 0 0 12px 12px; padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .tk-tag {{ display: inline-block; background: #e6f7f8; color: #006b70; padding: 2px 10px;
    border-radius: 20px; font-size: 0.8rem; margin: 2px; }}
  table.dataTable td {{ font-size: 0.85rem; vertical-align: middle; }}
  table.dataTable th {{ font-size: 0.85rem; }}
  .project-link {{ color: #006b70; text-decoration: none; font-weight: 500; }}
  .project-link:hover {{ text-decoration: underline; color: #008B92; }}
  /* Панель с кнопкой экспорта CSV */
  .dataTables_wrapper .dt-buttons {{
    display: inline-flex !important;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px 14px;
    margin: 0 0 12px 0;
    background: linear-gradient(180deg, #f8fafb 0%, #eef2f5 100%);
    border: 1px solid #d0d8e0;
    border-radius: 10px;
    box-shadow:
      inset 0 1px 0 rgba(255,255,255,0.9),
      0 1px 3px rgba(1,19,29,0.08);
  }}
  /* DataTables + Bootstrap 5 часто скрывают подпись кнопки — фиксируем явно */
  div.dt-buttons > button.dt-button.export-csv-btn,
  div.dt-buttons > div.dt-button.export-csv-btn {{
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    box-sizing: border-box !important;
    min-height: 38px !important;
    padding: 8px 16px !important;
    margin: 0 !important;
    border: 1px solid #006b70 !important;
    border-radius: 8px !important;
    background: #008B92 !important;
    background-image: none !important;
    color: #ffffff !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    line-height: 1.3 !important;
    text-indent: 0 !important;
    letter-spacing: normal !important;
    overflow: visible !important;
    width: auto !important;
    box-shadow: none !important;
  }}
  div.dt-buttons > button.dt-button.export-csv-btn:hover,
  div.dt-buttons > div.dt-button.export-csv-btn:hover {{
    background: #016b70 !important;
    border-color: #015a5f !important;
    color: #fff !important;
  }}
  div.dt-buttons > button.dt-button.export-csv-btn span,
  div.dt-buttons > div.dt-button.export-csv-btn span {{
    color: inherit !important;
    font-size: 14px !important;
    line-height: inherit !important;
  }}
  div.dt-buttons .dt-button.export-csv-btn {{
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    background: #008B92 !important;
    color: #fff !important;
    border-radius: 8px !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    padding: 8px 16px !important;
    text-indent: 0 !important;
  }}
</style>
</head>
<body>
<div class="container-fluid mt-3">
<div class="d-flex justify-content-end align-items-center mb-2">
  <span class="text-muted" style="font-size:0.85rem;">Обновлено: {last_updated}</span>
</div>

<!-- Вкладки -->
<ul class="nav nav-tabs" id="dashTabs" role="tablist">
  <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#overview" role="tab">Обзор</a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#gost" role="tab">ГОСТы <span class="badge" style="background:#008B92;">{stats['total_gost']}</span></a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#sp" role="tab">Своды правил <span class="badge" style="background:#008B92;">{stats['total_sp']}</span></a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#our-tks" role="tab">Участвуем в ТК</a></li>
</ul>

<div class="tab-content" id="dashTabContent">

<!-- ======================== ОБЗОР ======================== -->
<div class="tab-pane fade show active" id="overview" role="tabpanel">
  <div class="row g-3 mt-1">
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#008B92;">{stats['total_gost']}</div>
        <div class="stat-label">ГОСТов в 2026</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#01313D;">{stats['total_sp']}</div>
        <div class="stat-label">Сводов правил</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#008B92;">{stats['active_count']}</div>
        <div class="stat-label">Активных обсуждений</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#95a5a6;">{stats['completed_count']}</div>
        <div class="stat-label">Завершённых</div>
      </div>
    </div>
  </div>

  <div class="row g-3 mt-2">
    <div class="col-md-7">
      <div class="chart-container">
        <h6 class="text-center text-muted mb-3">Разбивка ГОСТов по техническим комитетам</h6>
        <div style="height:{chart_height}px;"><canvas id="tkChart"></canvas></div>
      </div>
    </div>
    <div class="col-md-5">
      <div class="chart-container" style="padding:14px;">
        <h6 class="text-center text-muted mb-2" style="font-size:0.8rem;">Участвуем в ТК vs все ГОСТы</h6>
        <div style="max-width:220px;margin:0 auto;"><canvas id="ourTkChart"></canvas></div>
      </div>
      <div class="chart-container" style="padding:14px;">
        <h6 class="text-center text-muted mb-2" style="font-size:0.8rem;">Разбивка «профильных» ГОСТов по ТК</h6>
        <div style="max-width:220px;margin:0 auto;"><canvas id="ourTkBreakdownChart"></canvas></div>
      </div>
      <div class="chart-container" style="padding:14px;">
        <h6 class="text-center text-muted mb-2" style="font-size:0.8rem;">Полимерные ГОСТы — комментарии</h6>
        <div style="max-width:220px;margin:0 auto;"><canvas id="polymerChart"></canvas></div>
      </div>
    </div>
  </div>
</div>

<!-- ======================== ГОСТы ======================== -->
<div class="tab-pane fade" id="gost" role="tabpanel">
  <div class="mt-3">
    <table id="gostTable" class="table table-striped table-hover" style="width:100%">
      <thead>
        <tr>
          <th>#</th>
          <th>Код ПРНС</th>
          <th>Тип</th>
          <th>Наименование проекта</th>
          <th>Технический комитет</th>
          <th>Разработчик</th>
          <th>Начало</th>
          <th>Завершение</th>
          <th>Статус</th>
        </tr>
      </thead>
    </table>
  </div>
</div>

<!-- ======================== СП ======================== -->
<div class="tab-pane fade" id="sp" role="tabpanel">
  <div class="mt-3">
    <table id="spTable" class="table table-striped table-hover" style="width:100%">
      <thead>
        <tr>
          <th>#</th>
          <th>Номер документа</th>
          <th>Тип уведомления</th>
          <th>Наименование проекта</th>
          <th>Разработчик</th>
          <th>Дата</th>
        </tr>
      </thead>
    </table>
  </div>
</div>

<!-- ======================== НАШИ ТК ======================== -->
<div class="tab-pane fade" id="our-tks" role="tabpanel">
  <div class="mt-3">
    <div class="mb-3">
      <strong>Участвуем в технических комитетах:</strong>
      <span id="ourTkList">{my_tks_display}</span>
    </div>
    <div id="ourTkSummary" class="row g-2 mb-3"></div>
    <table id="ourTkTable" class="table table-striped table-hover" style="width:100%">
      <thead>
        <tr>
          <th>#</th>
          <th>Код ПРНС</th>
          <th>Тип</th>
          <th>Наименование проекта</th>
          <th>Технический комитет</th>
          <th>Разработчик</th>
          <th>Начало</th>
          <th>Завершение</th>
          <th>Статус</th>
        </tr>
      </thead>
    </table>
  </div>
</div>

</div><!-- tab-content -->
</div><!-- container -->

<!-- Данные -->
<script>const D={data_json};</script>

<!-- CDN -->
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>
<script src="https://cdn.datatables.net/1.13.8/js/dataTables.bootstrap5.min.js"></script>
<script src="https://cdn.datatables.net/buttons/2.4.2/js/dataTables.buttons.min.js"></script>
<script src="https://cdn.datatables.net/buttons/2.4.2/js/buttons.bootstrap5.min.js"></script>
<script src="https://cdn.datatables.net/buttons/2.4.2/js/buttons.html5.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<script>
const MY_TKS = {my_tks_json};
const STATS = {stats_json};
const STATUS_COLORS = {status_colors};
const POLYMER_STATS = {polymer_stats_json};

function fixCsvExportButtons(dtApi) {{
  if (!dtApi || !dtApi.buttons) {{
    return;
  }}
  dtApi.buttons().nodes().each(function () {{
    var el = this;
    if (!el || !el.classList || !el.classList.contains('export-csv-btn')) {{
      return;
    }}
    el.classList.remove('btn-secondary');
    var t = (el.textContent || el.innerText || '').trim();
    if (!t) {{
      el.textContent = 'Экспорт CSV';
    }}
  }});
}}

function isMyTk(tc) {{
  if (!MY_TKS.length) return false;
  const tcL = (tc||'').toLowerCase();
  return MY_TKS.some(tk => tcL.includes(tk.toLowerCase()));
}}

function statusBadge(s) {{
  const m = {{
    'Вынесен на публичное обсуждение':'badge-active',
    'Публичное обсуждение завершено':'badge-completed',
    'Продлен срок публичного обсуждения':'badge-extended',
    'На доработке':'badge-revision',
    'Направлено уведомление о завершении публичного обсуждения':'badge-notification'
  }};
  const c = m[s]||'bg-secondary';
  // Short label
  const labels = {{
    'Вынесен на публичное обсуждение':'Публ. обсуждение',
    'Публичное обсуждение завершено':'Завершено',
    'Продлен срок публичного обсуждения':'Продлено',
    'На доработке':'Доработка',
    'Направлено уведомление о завершении публичного обсуждения':'Уведомление'
  }};
  return '<span class="badge '+c+'">'+( labels[s]||s )+'</span>';
}}

$(document).ready(function() {{
  // ГОСТ таблица
  const gostCols = [
    {{ data: null, render: (d,t,r,m) => m.row+1, orderable:false, width:'30px' }},
    {{ data: 'prns_code', defaultContent:'—', width:'140px',
      render: d => '<code style="font-size:0.8rem;">'+(d||'—')+'</code>' }},
    {{ data: 'doc_type', width:'80px' }},
    {{ data: 'project_name', render: (d,t,row) =>
      '<a href="'+row.url+'" target="_blank" class="project-link">'+d+'</a>' }},
    {{ data: 'technical_committee', render: d => {{
      const hl = isMyTk(d) ? ' <span class="badge text-white" style="font-size:0.65rem;background:#FC5A41;">наш ТК</span>' : '';
      return d + hl;
    }} }},
    {{ data: 'developer' }},
    {{ data: 'start_date', width:'90px' }},
    {{ data: 'end_date', width:'90px' }},
    {{ data: 'status', render: d => statusBadge(d), width:'120px' }}
  ];

  $('#gostTable').DataTable({{
    data: D.gost,
    columns: gostCols,
    deferRender: true,
    pageLength: 50,
    order: [[6, 'desc']],
    createdRow: function(row, data) {{
      if (isMyTk(data.technical_committee)) $(row).addClass('tk-highlight');
    }},
    language: {{ url: 'https://cdn.datatables.net/plug-ins/1.13.8/i18n/ru.json' }},
    dom: '<"row"<"col-sm-6"B><"col-sm-6"f>>rtip',
    initComplete: function () {{
      fixCsvExportButtons(this.api());
    }},
    buttons: [{{
      extend: 'csvHtml5',
      text: 'Экспорт CSV',
      className: 'export-csv-btn',
      bom: true,
      charset: 'utf-8',
      filename: 'gost_notifications',
      title: ''
    }}]
  }});

  // СП таблица
  const spCols = [
    {{ data: null, render: (d,t,r,m) => m.row+1, orderable:false, width:'30px' }},
    {{ data: 'title', defaultContent:'—', render: d => '<span style="font-size:0.8rem;">'+(d||'—')+'</span>' }},
    {{ data: 'notification_type', defaultContent:'—' }},
    {{ data: 'project_name', render: (d,t,row) => {{
      const name = d || row.title || 'Без названия';
      return '<a href="'+row.url+'" target="_blank" class="project-link">'+name+'</a>';
    }} }},
    {{ data: 'developer', defaultContent:'—' }},
    {{ data: 'placement_date', defaultContent:'—', render: d => d || '—', width:'90px' }}
  ];

  $('#spTable').DataTable({{
    data: D.sp,
    columns: spCols,
    deferRender: true,
    pageLength: 50,
    order: [[5, 'desc']],
    language: {{ url: 'https://cdn.datatables.net/plug-ins/1.13.8/i18n/ru.json' }},
    dom: '<"row"<"col-sm-6"B><"col-sm-6"f>>rtip',
    initComplete: function () {{
      fixCsvExportButtons(this.api());
    }},
    buttons: [{{
      extend: 'csvHtml5',
      text: 'Экспорт CSV',
      className: 'export-csv-btn',
      bom: true,
      charset: 'utf-8',
      filename: 'sp_notifications',
      title: ''
    }}]
  }});

  // Участвуем в ТК — фильтрованная таблица
  const myTkData = D.gost.filter(r => isMyTk(r.technical_committee));

  // Сводка по каждому ТК
  if (MY_TKS.length) {{
    const summary = {{}};
    myTkData.forEach(r => {{
      const tk = r.technical_committee;
      if (!summary[tk]) summary[tk] = {{total:0, active:0}};
      summary[tk].total++;
      if (r.status === 'Вынесен на публичное обсуждение') summary[tk].active++;
    }});
    let html = '';
    Object.keys(summary).sort().forEach(tk => {{
      const s = summary[tk];
      html += '<div class="col-md-4"><div class="stat-card" style="padding:12px;text-align:left;">'
        + '<strong style="color:#01313D;">'+tk+'</strong><br>'
        + '<span class="badge badge-active">'+s.active+' акт.</span> '
        + '<span class="text-muted">из '+s.total+' всего</span>'
        + '</div></div>';
    }});
    $('#ourTkSummary').html(html);
  }} else {{
    $('#ourTkSummary').html('<div class="alert alert-info">Добавьте ваши ТК в файл <code>data/my_technical_committees.json</code></div>');
  }}

  $('#ourTkTable').DataTable({{
    data: myTkData,
    columns: gostCols,
    deferRender: true,
    pageLength: 50,
    order: [[6, 'desc']],
    createdRow: function(row) {{ $(row).addClass('tk-highlight'); }},
    language: {{ url: 'https://cdn.datatables.net/plug-ins/1.13.8/i18n/ru.json' }},
    dom: '<"row"<"col-sm-6"B><"col-sm-6"f>>rtip',
    initComplete: function () {{
      fixCsvExportButtons(this.api());
    }},
    buttons: [{{
      extend: 'csvHtml5',
      text: 'Экспорт CSV',
      className: 'export-csv-btn',
      bom: true,
      charset: 'utf-8',
      filename: 'our_tk_notifications',
      title: ''
    }}]
  }});

  // Графики
  // 1. Все ГОСТы по ТК (Horizontal Bar)
  const _tkVals = (STATS.all_tk_values || []).map(Number);
  const _tkMax = _tkVals.length ? Math.max.apply(null, _tkVals) : 0;
  const _tkXSuggestedMax = _tkMax + Math.max(2, Math.ceil(_tkMax * 0.14));

  new Chart(document.getElementById('tkChart'), {{
    type: 'bar',
    data: {{
      labels: STATS.all_tk_labels,
      datasets: [{{
        label: 'Уведомлений',
        data: STATS.all_tk_values,
        backgroundColor: STATS.all_tk_labels.map(tk =>
          MY_TKS.some(t => tk.toLowerCase().includes(t.toLowerCase())) ? '#FC5A41' : '#008B92'
        ),
        borderRadius: 4
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      layout: {{
        padding: {{ right: 28, left: 4 }}
      }},
      plugins: {{
        legend: {{ display: false }},
        datalabels: {{
          display: true,
          anchor: 'end',
          align: 'right',
          offset: 8,
          color: '#01313D',
          formatter: function(value) {{ return value; }},
          font: {{ size: 11, weight: '600' }},
          clamp: true
        }},
        tooltip: {{
          callbacks: {{
            label: ctx => ctx.parsed.x + ' уведомл.'
          }}
        }}
      }},
      scales: {{
        x: {{
          beginAtZero: true,
          suggestedMax: _tkXSuggestedMax,
          grace: '12%',
          ticks: {{ stepSize: 1 }}
        }},
        y: {{ ticks: {{ font: {{ size: 11 }} }} }}
      }}
    }},
    plugins: [ChartDataLabels]
  }});

  // 2. Участвуем в ТК vs остальные (Doughnut)
  const myTkCount = D.gost.filter(r => isMyTk(r.technical_committee)).length;
  const otherCount = D.gost.length - myTkCount;
  const hasMyTks = MY_TKS.length > 0;

  if (hasMyTks) {{
    new Chart(document.getElementById('ourTkChart'), {{
      type: 'doughnut',
      data: {{
        labels: ['Участвуем в ТК (' + myTkCount + ')', 'Остальные ТК (' + otherCount + ')'],
        datasets: [{{
          data: [myTkCount, otherCount],
          backgroundColor: ['#FC5A41', '#008B92']
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ font: {{ size: 10 }}, boxWidth: 10, padding: 8 }} }},
          tooltip: {{
            callbacks: {{
              label: function(ctx) {{
                const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
                const pct = total > 0 ? Math.round(ctx.parsed / total * 100) : 0;
                return ctx.parsed + ' (' + pct + '%)';
              }}
            }}
          }},
          datalabels: {{
            color: '#fff',
            font: {{ size: 12, weight: 'bold' }},
            formatter: function(value, ctx) {{
              const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
              const pct = total > 0 ? Math.round(value / total * 100) : 0;
              return value + '\\n(' + pct + '%)';
            }},
            textAlign: 'center'
          }}
        }}
      }},
      plugins: [ChartDataLabels]
    }});

    // 3. Разбивка «профильных» ГОСТов по ТК (Doughnut)
    const myTkBreakdown = {{}};
    D.gost.filter(r => isMyTk(r.technical_committee)).forEach(r => {{
      const tk = r.technical_committee || 'Не указан';
      myTkBreakdown[tk] = (myTkBreakdown[tk] || 0) + 1;
    }});
    const bLabels = Object.keys(myTkBreakdown).sort((a,b) => myTkBreakdown[b] - myTkBreakdown[a]);
    const bValues = bLabels.map(k => myTkBreakdown[k]);
    const tkPalette = [
      '#008B92','#FC5A41','#01313D','#2ecc71','#e67e22','#9b59b6',
      '#3498db','#e74c3c','#1abc9c','#f39c12','#d35400','#8e44ad',
      '#16a085','#c0392b','#27ae60','#2980b9','#f1c40f','#7f8c8d',
      '#2c3e50','#1dd1a1','#ff6b6b','#54a0ff','#5f27cd','#01a3a4'
    ];

    new Chart(document.getElementById('ourTkBreakdownChart'), {{
      type: 'doughnut',
      data: {{
        labels: bLabels.map((l,i) => l.substring(0,40) + ' (' + bValues[i] + ')'),
        datasets: [{{
          data: bValues,
          backgroundColor: bLabels.map((_, i) => tkPalette[i % tkPalette.length])
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ font: {{ size: 10 }}, boxWidth: 10, padding: 8 }} }},
          tooltip: {{
            callbacks: {{
              label: function(ctx) {{
                const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
                const pct = total > 0 ? Math.round(ctx.parsed / total * 100) : 0;
                return ctx.parsed + ' (' + pct + '%)';
              }}
            }}
          }},
          datalabels: {{
            color: '#fff',
            font: {{ size: 12, weight: 'bold' }},
            formatter: function(value) {{ return value; }},
            display: function(ctx) {{ return ctx.dataset.data[ctx.dataIndex] >= 2; }}
          }}
        }}
      }},
      plugins: [ChartDataLabels]
    }});
  }} else {{
    const ctx = document.getElementById('ourTkChart');
    ctx.parentElement.innerHTML = '<div class="alert alert-info mt-4 text-center">'
      + '<strong>ТК не настроены</strong><br>'
      + 'Добавьте ваши ТК в файл <code>data/my_technical_committees.json</code><br>'
      + 'чтобы видеть статистику по интересующим комитетам.'
      + '</div>';
    document.getElementById('ourTkBreakdownChart').parentElement.style.display = 'none';
  }}

  // 4. Полимерные ГОСТы — комментарии (Doughnut)
  const polyTotal = POLYMER_STATS.total || 0;
  const polyCommented = POLYMER_STATS.commented || 0;
  const polyNoComment = polyTotal - polyCommented;

  if (polyTotal > 0) {{
    new Chart(document.getElementById('polymerChart'), {{
      type: 'doughnut',
      data: {{
        labels: [
          'Комментарий направлен (' + polyCommented + ')',
          'Без комментария (' + polyNoComment + ')'
        ],
        datasets: [{{
          data: [polyCommented, polyNoComment],
          backgroundColor: ['#2ecc71', '#e67e22']
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ font: {{ size: 10 }}, boxWidth: 10, padding: 8 }} }},
          tooltip: {{
            callbacks: {{
              label: function(ctx) {{
                const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
                const pct = total > 0 ? Math.round(ctx.parsed / total * 100) : 0;
                return ctx.parsed + ' (' + pct + '%)';
              }}
            }}
          }},
          datalabels: {{
            color: '#fff',
            font: {{ size: 12, weight: 'bold' }},
            formatter: function(value, ctx) {{
              const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
              const pct = total > 0 ? Math.round(value / total * 100) : 0;
              return value + '\\n(' + pct + '%)';
            }},
            textAlign: 'center'
          }}
        }}
      }},
      plugins: [ChartDataLabels]
    }});
  }} else {{
    const polyCtx = document.getElementById('polymerChart');
    polyCtx.parentElement.innerHTML = '<div class="alert alert-secondary mt-3 text-center" style="padding:30px;">'
      + '<i style="font-size:2rem; color:#95a5a6;">📋</i><br>'
      + '<strong>Полимерных ГОСТов пока нет</strong><br>'
      + '<span class="text-muted">Данные появятся после обнаружения полимерных стандартов</span>'
      + '</div>';
  }}

  // Lazy init DataTables on tab show
  $('a[data-bs-toggle="tab"]').on('shown.bs.tab', function(e) {{
    $.fn.dataTable.tables({{visible: true, api: true}}).columns.adjust();
  }});
}});
</script>
</body>
</html>"""
