#!/usr/bin/env python3
"""Веб-интерфейс мониторинга Росстандарта. 
Поддержка многолетности, московское время, корректная сортировка дат."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from collections import Counter
from datetime import datetime
from functools import wraps
from hmac import compare_digest
from zoneinfo import ZoneInfo  # Python 3.9+ для московского времени

from flask import Flask, redirect, render_template_string, request, session, url_for, flash
from dotenv import load_dotenv

load_dotenv()
from src.dashboard_config import DASHBOARD_REGISTRY_PATH

# ──────────────────────────────────────────────────────────────
# КОНФИГ
# ──────────────────────────────────────────────────────────────
APP_USERNAME = os.environ.get("APP_USERNAME", "polylab")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "2026")  # Default для локальной разработки
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY") or secrets.token_urlsafe(32)
APP_PORT = int(os.environ.get("PORT", "8000"))
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
MAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
RUN_TIMEOUT = int(os.environ.get("RUN_NOW_TIMEOUT_SECONDS", "600"))

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY
app.jinja_env.globals.update({
    'max': max, 'min': min, 'sum': sum, 'round': round,
    'len': len, 'int': int, 'float': float, 'str': str,
})

# ──────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (объявлены ДО использования)
# ──────────────────────────────────────────────────────────────

def _get_current_year() -> int:
    """Получает текущий год по московскому времени."""
    return datetime.now(ZoneInfo("Europe/Moscow")).year

def _extract_year_from_date(date_str: str) -> int | None:
    """Извлекает год из строки даты в различных форматах."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if "." in s:
        parts = s.split(".")
        if len(parts) == 3:
            try:
                return int(parts[2]) if len(parts[2]) == 4 else int("20" + parts[2])
            except ValueError:
                return None
        elif len(parts) == 2:
            try:
                return int(parts[1]) if len(parts[1]) == 4 else int("20" + parts[1])
            except ValueError:
                return None
    elif "-" in s:
        try:
            return int(s.split("-")[0])
        except ValueError:
            return None
    return None

def _is_current_year(date_str: str, year: int | None = None) -> bool:
    """Проверяет, относится ли дата к указанному году."""
    if year is None:
        year = _get_current_year()
    date_year = _extract_year_from_date(date_str)
    return date_year == year if date_year else False

def _parse_date_for_sorting(date_str: str) -> datetime | None:
    """Парсит дату для корректной сортировки."""
    if not date_str:
        return None
    s = str(date_str).strip()
    formats = ["%d.%m.%Y", "%m.%Y", "%Y-%m-%d", "%d.%m.%y"]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _format_moscow_time(dt: datetime | None = None) -> str:
    """Форматирует время в московском часовом поясе."""
    if dt is None:
        dt = datetime.now(ZoneInfo("Europe/Moscow"))
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    else:
        dt = dt.astimezone(ZoneInfo("Europe/Moscow"))
    return dt.strftime("%d.%m.%Y %H:%M")

def _load_committees() -> list[str]:
    """Загружает список технических комитетов из конфигурационного файла."""
    tk_path = os.path.join(os.path.dirname(DASHBOARD_REGISTRY_PATH), "my_technical_committees.json")
    if os.path.exists(tk_path):
        try:
            with open(tk_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("committees", [])
        except Exception:
            pass
    return []

def _compute_stats(gost_list: list[dict]) -> dict:
    """Вычисляет статистику по списку ГОСТов."""
    month_counter = Counter()
    tk_counter = Counter()
    status_counter = Counter()
    doc_type_counter = Counter()
    active = 0
    
    month_names = {"01":"Янв","02":"Фев","03":"Мар","04":"Апр","05":"Май","06":"Июн",
                   "07":"Июл","08":"Авг","09":"Сен","10":"Окт","11":"Ноя","12":"Дек"}
                   
    for g in gost_list:
        sd = g.get("start_date", "")
        if sd and "." in sd:
            parts = sd.split(".")
            if len(parts) == 3:
                month_counter[f"{parts[2]}-{parts[1]}"] += 1
                
        tk = g.get("technical_committee", "")
        if tk: tk_counter[tk] += 1
        
        st = g.get("status", "Неизвестно")
        status_counter[st] += 1
        if st == "Вынесен на публичное обсуждение": active += 1
        doc_type_counter[g.get("doc_type", "Не указан")] += 1

    sorted_months = sorted(month_counter.keys())
    m_labels = [f"{month_names.get(p[1], p[1])} {p[0]}" for p in [m.split("-") for m in sorted_months]]
    m_values = [month_counter[m] for m in sorted_months]
    
    tk_most = tk_counter.most_common()
    
    return {
        "total_gost": len(gost_list),
        "total_sp": 0,
        "active_count": active,
        "completed_count": len(gost_list) - active,
        "status_labels": list(status_counter.keys()),
        "status_values": list(status_counter.values()),
        "month_labels": m_labels,
        "month_values": m_values,
        "all_tk_labels": [t[0] for t in tk_most],
        "all_tk_values": [t[1] for t in tk_most],
        "doc_type_labels": list(doc_type_counter.keys()),
        "doc_type_values": list(doc_type_counter.values())
    }

def _prepare_dashboard_data() -> dict:
    """Подготавливает данные для дашборда."""
    if not os.path.exists(DASHBOARD_REGISTRY_PATH):
        return {
            "gost": [], "sp": [], "stats": {}, "my_tks": [], 
            "polymer": {"total": 0, "commented": 0}, 
            "updated": "—", "current_year": _get_current_year()
        }
        
    try:
        with open(DASHBOARD_REGISTRY_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {
            "gost": [], "sp": [], "stats": {}, "my_tks": [], 
            "polymer": {"total": 0, "commented": 0}, 
            "updated": "—", "current_year": _get_current_year()
        }

    current_year = _get_current_year()
    
    # ФИЛЬТРАЦИЯ: только данные за текущий год по start_date
    gost = []
    for r in raw.get("gost", []):
        start_date = r.get("start_date", "")
        if start_date and _is_current_year(start_date, current_year):
            gost.append(r)
    
    # СП: фильтрация по placement_date
    sp = []
    for r in raw.get("sp", []):
        placement_date = r.get("placement_date", "")
        if placement_date and _is_current_year(placement_date, current_year):
            sp.append(r)
    
    stats = _compute_stats(gost)
    stats["total_sp"] = len(sp)
    
    polymer_total = sum(1 for g in gost if g.get("matched_keywords"))
    polymer_commented = 0
    
    updated = raw.get("metadata", {}).get("last_updated", "")
    if updated:
        try:
            dt = datetime.fromisoformat(updated)
            updated = _format_moscow_time(dt)
        except: 
            updated = _format_moscow_time()
    else:
        updated = _format_moscow_time()

    return {
        "gost": gost,
        "sp": sp,
        "stats": stats,
        "my_tks": _load_committees(),
        "polymer": {"total": polymer_total, "commented": polymer_commented},
        "updated": updated,
        "current_year": current_year
    }

# ──────────────────────────────────────────────────────────────
# HTML ШАБЛОН (ТОЧНАЯ КОПИЯ ОРИГИНАЛА + ДИНАМИЧЕСКАЯ ПОДСТАНОВКА)
# ──────────────────────────────────────────────────────────────
DASHBOARD_TMPL = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Мониторинг Росстандарта {{ current_year }}</title>
<!-- Favicon -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📋</text></svg>">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.datatables.net/1.13.8/css/dataTables.bootstrap5.min.css" rel="stylesheet">
<link href="https://cdn.datatables.net/buttons/2.4.2/css/buttons.bootstrap5.min.css" rel="stylesheet">
<style>
body { background: #f4f6f8; font-family: 'Segoe UI', Arial, sans-serif; }

.app-header {
  background: linear-gradient(135deg, #008B92 0%, #01313D 100%);
  color: #fff;
  padding: 20px 0;
  margin-bottom: 20px;
  box-shadow: 0 2px 10px rgba(0,0,0,0.1);
}
.app-header h1 {
  margin: 0;
  font-size: 1.8rem;
  font-weight: 600;
  letter-spacing: 0.5px;
}
.app-header .subtitle {
  opacity: 0.9;
  font-size: 0.9rem;
  margin-top: 4px;
}
.stat-card { background: #fff; border-radius: 12px; padding: 20px; text-align: center;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08); transition: transform 0.2s;
  border-top: 3px solid #008B92; }
.stat-card:hover { transform: translateY(-2px); box-shadow: 0 4px 16px rgba(0,0,0,0.12); }
.stat-number { font-size: 2.2rem; font-weight: 700; }
.stat-label { color: #556; font-size: 0.85rem; margin-top: 4px; }
.chart-container { background: #fff; border-radius: 12px; padding: 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 20px; }
.tk-highlight { background-color: #e6f7f8 !important; border-left: 3px solid #008B92; }
.badge-active { background: #008B92; }
.badge-completed { background: #95a5a6; }
.badge-extended { background: #e67e22; }
.badge-revision { background: #8e44ad; }
.badge-notification { background: #01313D; }
.nav-tabs .nav-link { font-weight: 500; color: #555; }
.nav-tabs .nav-link.active { color: #01313D; border-color: #008B92 #008B92 #fff; font-weight: 600; }
.tab-content { background: #fff; border-radius: 0 0 12px 12px; padding: 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
.tk-tag { display: inline-block; background: #e6f7f8; color: #006b70; padding: 2px 10px;
  border-radius: 20px; font-size: 0.8rem; margin: 2px; }
table.dataTable td { font-size: 0.85rem; vertical-align: middle; }
table.dataTable th { font-size: 0.85rem; }
.project-link { color: #006b70; text-decoration: none; font-weight: 500; }
.project-link:hover { text-decoration: underline; color: #008B92; }
.dataTables_wrapper .dt-buttons {
  display: inline-flex !important; align-items: center; flex-wrap: wrap; gap: 8px;
  padding: 10px 14px; margin: 0 0 12px 0; background: linear-gradient(180deg, #f8fafb 0%, #eef2f5 100%);
  border: 1px solid #d0d8e0; border-radius: 10px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.9), 0 1px 3px rgba(1,19,29,0.08);
}
div.dt-buttons > button.dt-button.export-csv-btn,
div.dt-buttons > div.dt-button.export-csv-btn {
  display: inline-flex !important; align-items: center !important; justify-content: center !important;
  box-sizing: border-box !important; min-height: 38px !important; padding: 8px 16px !important;
  margin: 0 !important; border: 1px solid #006b70 !important; border-radius: 8px !important;
  background: #008B92 !important; background-image: none !important; color: #ffffff !important;
  font-size: 14px !important; font-weight: 600 !important; line-height: 1.3 !important;
  text-indent: 0 !important; letter-spacing: normal !important; overflow: visible !important;
  width: auto !important; box-shadow: none !important;
}
div.dt-buttons > button.dt-button.export-csv-btn:hover,
div.dt-buttons > div.dt-button.export-csv-btn:hover {
  background: #016b70 !important; border-color: #015a5f !important; color: #fff !important;
}
.msg { position: fixed; top: 15px; right: 15px; z-index: 9999; min-width: 250px; }

.update-timestamp {
  text-align: right;
  color: #6c757d;
  font-size: 0.85rem;
  white-space: nowrap;
}
</style>
</head>
<body>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
  <div class="msg">
    {% for category, text in messages %}
      <div class="alert alert-{{ 'success' if category == 'success' else 'danger' }} alert-dismissible fade show shadow-sm" role="alert">
        {{ text }}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
      </div>
    {% endfor %}
  </div>
  <script>
    setTimeout(() => {
      document.querySelectorAll('.alert').forEach(el => {
        const bs = bootstrap.Alert.getOrCreateInstance(el);
        if(bs) bs.close();
      });
    }, 2000);
  </script>
  {% endif %}
{% endwith %}

<!-- Красивый хедер -->
<div class="app-header">
  <div class="container-fluid">
    <div class="row align-items-center">
      <div class="col-md-8">
        <h1>Мониторинг Росстандарта {{ current_year }}</h1>
        <div class="subtitle">Система отслеживания новых ГОСТов и сводов правил</div>
      </div>
    </div>
  </div>
</div>

<div class="container-fluid">
<div class="row mb-3">
  <div class="col-md-6">
    <form id="run-form" method="post" action="{{ url_for('run_now') }}" style="margin:0">
      <button id="run-btn" class="btn btn-outline-secondary btn-sm">🔄 Обновить данные</button>
    </form>
  </div>
  <div class="col-md-6">
    <div class="update-timestamp">
      <strong>Обновлено:</strong> {{ last_updated }}
    </div>
  </div>
</div>

<ul class="nav nav-tabs" id="dashTabs" role="tablist">
  <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#overview" role="tab">Обзор</a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#gost" role="tab">ГОСТы <span class="badge" style="background:#008B92;">{{ stats.total_gost }}</span></a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#sp" role="tab">Своды правил <span class="badge" style="background:#008B92;">{{ stats.total_sp }}</span></a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#our-tks" role="tab">Участвуем в ТК</a></li>
</ul>

<div class="tab-content" id="dashTabContent">

<div class="tab-pane fade show active" id="overview" role="tabpanel">
  <div class="row g-3 mt-1">
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#008B92;">{{ stats.total_gost }}</div>
        <div class="stat-label">ГОСТов в {{ current_year }}</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#01313D;">{{ stats.total_sp }}</div>
        <div class="stat-label">Сводов правил</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#008B92;">{{ stats.active_count }}</div>
        <div class="stat-label">Активных обсуждений</div>
      </div>
    </div>
    <div class="col-md-3">
      <div class="stat-card">
        <div class="stat-number" style="color:#95a5a6;">{{ stats.completed_count }}</div>
        <div class="stat-label">Завершённых</div>
      </div>
    </div>
  </div>

  <div class="row g-3 mt-2">
    <div class="col-md-7">
      <div class="chart-container">
        <h6 class="text-center text-muted mb-3">Разбивка ГОСТов по техническим комитетам</h6>
        <div style="height:{{ max(400, stats.all_tk_labels|length * 28) }}px;"><canvas id="tkChart"></canvas></div>
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
      <span id="ourTkList">{{ my_tks|join(', ') or 'Не настроено' }}</span>
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
<script>
const D = {
  gost: {{ gost_json|safe }},
  sp: {{ sp_json|safe }},
  myTechnicalCommittees: {{ my_tks_json|safe }},
  stats: {{ stats_json|safe }},
  lastUpdated: "{{ last_updated }}",
  polymerStats: {{ polymer_json|safe }},
  currentYear: {{ current_year }}
};
</script>

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
const MY_TKS = D.myTechnicalCommittees;
const STATS = D.stats;
const STATUS_COLORS = ['#008B92','#FC5A41','#e67e22','#8e44ad','#01313D','#95a5a6'];
const POLYMER_STATS = D.polymerStats;

// Функция парсинга даты для сортировки
function parseDate(dateStr) {
  if (!dateStr) return new Date(0);
  const s = dateStr.trim();
  // Пробуем разные форматы
  let parts;
  if (s.includes('.')) {
    parts = s.split('.');
    if (parts.length === 3) {
      // DD.MM.YYYY
      return new Date(parseInt(parts[2]), parseInt(parts[1]) - 1, parseInt(parts[0]));
    } else if (parts.length === 2) {
      // MM.YYYY
      return new Date(parseInt(parts[1]), parseInt(parts[0]) - 1, 1);
    }
  } else if (s.includes('-')) {
    // YYYY-MM-DD
    return new Date(s);
  }
  return new Date(0);
}

function fixCsvExportButtons(dtApi) {
  if (!dtApi || !dtApi.buttons) return;
  dtApi.buttons().nodes().each(function () {
    var el = this;
    if (!el || !el.classList || !el.classList.contains('export-csv-btn')) return;
    el.classList.remove('btn-secondary');
    var t = (el.textContent || el.innerText || '').trim();
    if (!t) el.textContent = 'Экспорт CSV';
  });
}
function isMyTk(tc) {
  if (!MY_TKS.length) return false;
  const tcL = (tc||'').toLowerCase();
  return MY_TKS.some(tk => tcL.includes(tk.toLowerCase()));
}
function statusBadge(s) {
  const m = {
    'Вынесен на публичное обсуждение':'badge-active',
    'Публичное обсуждение завершено':'badge-completed',
    'Продлен срок публичного обсуждения':'badge-extended',
    'На доработке':'badge-revision',
    'Направлено уведомление о завершении публичного обсуждения':'badge-notification'
  };
  const c = m[s]||'bg-secondary';
  const labels = {
    'Вынесен на публичное обсуждение':'Публ. обсуждение',
    'Публичное обсуждение завершено':'Завершено',
    'Продлен срок публичного обсуждения':'Продлено',
    'На доработке':'Доработка',
    'Направлено уведомление о завершении публичного обсуждения':'Уведомление'
  };
  return '<span class="badge '+c+'">'+(labels[s]||s)+'</span>';
}

$(document).ready(function() {
  // Отладка: выводим информацию о данных
  console.log('📊 Данные дашборда:', {
    gost_count: D.gost?.length || 0,
    sp_count: D.sp?.length || 0,
    current_year: D.currentYear,
    first_gost: D.gost?.[0]
  });

  // === ГОСТ ТАБЛИЦА ===
  const gostTable = $('#gostTable').DataTable({
    data: D.gost, 
    columns: [
      { data: null, render: (d,t,r,m) => m.row+1, orderable: false, width:'30px' },
      { data: 'prns_code', defaultContent:'—', width:'140px', render: d => '<code style="font-size:0.8rem;">'+(d||'—')+'</code>' },
      { data: 'doc_type', width:'80px' },
      { data: 'project_name', render: (d,t,row) => '<a href="'+row.url+'" target="_blank" class="project-link">'+d+'</a>' },
      { data: 'technical_committee', render: d => {
        const hl = isMyTk(d) ? ' <span class="badge text-white" style="font-size:0.65rem;background:#FC5A41;">наш ТК</span>' : '';
        return d + hl;
      }},
      { data: 'developer' },
      { data: 'start_date', width:'90px', render: d => d || '—' },
      { data: 'end_date', width:'90px', render: d => d || '—' },
      { data: 'status', render: d => statusBadge(d), width:'120px' }
    ],
    deferRender: true, 
    pageLength: 50, 
    order: [[6, 'desc']],
    orderCellsTop: true,// Сортировка по start_date по убыванию
    createdRow: function(row, data) { 
      if (isMyTk(data.technical_committee)) $(row).addClass('tk-highlight'); 
    },
    language: { url: 'https://cdn.datatables.net/plug-ins/1.13.8/i18n/ru.json' },
    dom: '<"row"<"col-sm-6"B><"col-sm-6"f>>rtip',
    initComplete: function () { fixCsvExportButtons(this.api()); },
    buttons: [{ 
      extend: 'csvHtml5', 
      text: 'Экспорт CSV', 
      className: 'export-csv-btn', 
      bom: true, 
      charset: 'utf-8', 
      filename: 'gost_notifications', 
      title: '' 
    }],
    columnDefs: [
      {
        targets: [6, 7],  // start_date и end_date
        type: 'date-eu',  // Европейский формат даты (DD.MM.YYYY)
        render: function(data, type, row) {
          if (type === 'sort' || type === 'type') {
            // Для сортировки преобразуем в timestamp
            return parseDate(data).getTime();
          }
          return data || '—';
        }
      }
    ]
  });

  // === СП ТАБЛИЦА ===
  const spTable = $('#spTable').DataTable({
    data: D.sp, 
    columns: [
      { data: null, render: (d,t,r,m) => m.row+1, orderable: false, width:'30px' },
      { data: 'title', defaultContent:'—', render: d => '<span style="font-size:0.8rem;">'+(d||'—')+'</span>' },
      { data: 'notification_type', defaultContent:'—' },
      { data: 'project_name', render: (d,t,row) => {
        const name = d || row.title || 'Без названия';
        return '<a href="'+row.url+'" target="_blank" class="project-link">'+name+'</a>';
      }},
      { data: 'developer', defaultContent:'—' },
      { data: 'placement_date', defaultContent:'—', render: d => d || '—', width:'90px' }
    ],
    deferRender: true, 
    pageLength: 50, 
    order: [[5, 'desc']],  // Сортировка по placement_date
    language: { url: 'https://cdn.datatables.net/plug-ins/1.13.8/i18n/ru.json' },
    dom: '<"row"<"col-sm-6"B><"col-sm-6"f>>rtip',
    initComplete: function () { fixCsvExportButtons(this.api()); },
    buttons: [{ 
      extend: 'csvHtml5', 
      text: 'Экспорт CSV', 
      className: 'export-csv-btn', 
      bom: true, 
      charset: 'utf-8', 
      filename: 'sp_notifications', 
      title: '' 
    }],
    columnDefs: [
      {
        targets: [5],  // placement_date
        type: 'date-eu',
        render: function(data, type, row) {
          if (type === 'sort' || type === 'type') {
            return parseDate(data).getTime();
          }
          return data || '—';
        }
      }
    ]
  });

  // === ТАБЛИЦА "НАШИ ТК" ===
  const myTkData = D.gost.filter(r => isMyTk(r.technical_committee));
  
  if (MY_TKS.length) {
    const summary = {};
    myTkData.forEach(r => {
      const tk = r.technical_committee;
      if (!summary[tk]) summary[tk] = {total:0, active:0};
      summary[tk].total++;
      if (r.status === 'Вынесен на публичное обсуждение') summary[tk].active++;
    });
    let html = '';
    Object.keys(summary).sort().forEach(tk => {
      const s = summary[tk];
      html += '<div class="col-md-4"><div class="stat-card" style="padding:12px;text-align:left;">'
        + '<strong style="color:#01313D;">'+tk+'</strong><br>'
        + '<span class="badge badge-active">'+s.active+' акт.</span> '
        + '<span class="text-muted">из '+s.total+' всего</span></div></div>';
    });
    $('#ourTkSummary').html(html);
  } else {
    $('#ourTkSummary').html('<div class="alert alert-info">Добавьте ваши ТК в файл <code>data/my_technical_committees.json</code></div>');
  }

  const ourTkTable = $('#ourTkTable').DataTable({
    data: myTkData, 
    columns: [
      { data: null, render: (d,t,r,m) => m.row+1, orderable: false, width:'30px' },
      { data: 'prns_code', defaultContent:'—', width:'140px', render: d => '<code style="font-size:0.8rem;">'+(d||'—')+'</code>' },
      { data: 'doc_type', width:'80px' },
      { data: 'project_name', render: (d,t,row) => '<a href="'+row.url+'" target="_blank" class="project-link">'+d+'</a>' },
      { data: 'technical_committee', render: d => {
        const hl = isMyTk(d) ? ' <span class="badge text-white" style="font-size:0.65rem;background:#FC5A41;">наш ТК</span>' : '';
        return d + hl;
      }},
      { data: 'developer' },
      { data: 'start_date', width:'90px', render: d => d || '—' },
      { data: 'end_date', width:'90px', render: d => d || '—' },
      { data: 'status', render: d => statusBadge(d), width:'120px' }
    ],
    deferRender: true, 
    pageLength: 50, 
    order: [[6, 'desc']],
    createdRow: function(row) { $(row).addClass('tk-highlight'); },
    language: { url: 'https://cdn.datatables.net/plug-ins/1.13.8/i18n/ru.json' },
    dom: '<"row"<"col-sm-6"B><"col-sm-6"f>>rtip',
    initComplete: function () { fixCsvExportButtons(this.api()); },
    buttons: [{ 
      extend: 'csvHtml5', 
      text: 'Экспорт CSV', 
      className: 'export-csv-btn', 
      bom: true, 
      charset: 'utf-8', 
      filename: 'our_tk_notifications', 
      title: '' 
    }],
    columnDefs: [
      {
        targets: [6, 7],
        type: 'date-eu',
        render: function(data, type, row) {
          if (type === 'sort' || type === 'type') {
            return parseDate(data).getTime();
          }
          return data || '—';
        }
      }
    ]
  });

  // === ГРАФИКИ ===
  if (!STATS || !STATS.all_tk_labels) {
    console.warn('⚠ Нет данных для графиков:', STATS);
  } else {
    const _tkVals = (STATS.all_tk_values || []).map(Number);
    const _tkMax = _tkVals.length ? Math.max(..._tkVals) : 0;
    const _tkXSuggestedMax = _tkMax + Math.max(2, Math.ceil(_tkMax * 0.14));

    // График ТК
    const tkCtx = document.getElementById('tkChart');
    if (tkCtx && STATS.all_tk_labels?.length > 0) {
      new Chart(tkCtx, {
        type: 'bar', 
        data: { 
          labels: STATS.all_tk_labels, 
          datasets: [{ 
            label: 'Уведомлений', 
            data: STATS.all_tk_values,
            backgroundColor: STATS.all_tk_labels.map(tk => 
              MY_TKS.some(t => tk.toLowerCase().includes(t.toLowerCase())) ? '#FC5A41' : '#008B92'
            ), 
            borderRadius: 4 
          }] 
        },
        options: { 
          indexAxis: 'y', 
          responsive: true, 
          maintainAspectRatio: false, 
          layout: { padding: { right: 28, left: 4 } },
          plugins: { 
            legend: { display: false }, 
            datalabels: { 
              display: true, anchor: 'end', align: 'right', offset: 8, 
              color: '#01313D', formatter: v => v, 
              font: { size: 11, weight: '600' }, clamp: true 
            }, 
            tooltip: { callbacks: { label: ctx => ctx.parsed.x + ' уведомл.' } } 
          },
          scales: { 
            x: { beginAtZero: true, suggestedMax: _tkXSuggestedMax, grace: '12%', ticks: { stepSize: 1 } }, 
            y: { ticks: { font: { size: 11 } } } 
          } 
        }, 
        plugins: [ChartDataLabels]
      });
    }

    // Круговая: Участвуем в ТК
    const myTkCount = D.gost.filter(r => isMyTk(r.technical_committee)).length;
    const otherCount = D.gost.length - myTkCount;
    const hasMyTks = MY_TKS.length > 0;
    const ourTkCtx = document.getElementById('ourTkChart');

    if (hasMyTks && ourTkCtx) {
      new Chart(ourTkCtx, {
        type: 'doughnut', 
        data: { 
          labels: ['Участвуем в ТК (' + myTkCount + ')', 'Остальные ТК (' + otherCount + ')'], 
          datasets: [{ 
            data: [myTkCount, otherCount], 
            backgroundColor: ['#FC5A41', '#008B92'] 
          }] 
        },
        options: { 
          responsive: true, 
          plugins: { 
            legend: { position: 'bottom', labels: { font: { size: 10 }, boxWidth: 10, padding: 8 } }, 
            tooltip: { 
              callbacks: { 
                label: function(ctx) { 
                  const total = ctx.dataset.data.reduce((a,b)=>a+b,0); 
                  const pct = total>0 ? Math.round(ctx.parsed/total*100) : 0; 
                  return ctx.parsed+' ('+pct+'%)'; 
                } 
              } 
            }, 
            datalabels: { 
              color: '#fff', font: { size: 12, weight: 'bold' }, 
              formatter: function(value, ctx) { 
                const total = ctx.dataset.data.reduce((a,b)=>a+b,0); 
                const pct = total>0 ? Math.round(value/total*100) : 0; 
                return value+'\\n('+pct+'%)'; 
              }, 
              textAlign: 'center' 
            } 
          } 
        }, 
        plugins: [ChartDataLabels]
      });

      // Круговая: Разбивка по ТК
      const myTkBreakdown = {};
      D.gost.filter(r => isMyTk(r.technical_committee)).forEach(r => { 
        const tk = r.technical_committee || 'Не указан'; 
        myTkBreakdown[tk] = (myTkBreakdown[tk]||0) + 1; 
      });
      const bLabels = Object.keys(myTkBreakdown).sort((a,b) => myTkBreakdown[b] - myTkBreakdown[a]);
      const bValues = bLabels.map(k => myTkBreakdown[k]);
      const tkPalette = ['#008B92','#FC5A41','#01313D','#2ecc71','#e67e22','#9b59b6','#3498db','#e74c3c','#1abc9c','#f39c12','#d35400','#8e44ad','#16a085','#c0392b','#27ae60','#2980b9','#f1c40f','#7f8c8d','#2c3e50','#1dd1a1','#ff6b6b','#54a0ff','#5f27cd','#01a3a4'];
      const breakdownCtx = document.getElementById('ourTkBreakdownChart');

      if (breakdownCtx && bLabels.length > 0) {
        new Chart(breakdownCtx, {
          type: 'doughnut', 
          data: { 
            labels: bLabels.map((l,i) => l.substring(0,40) + ' (' + bValues[i] + ')'), 
            datasets: [{ 
              data: bValues, 
              backgroundColor: bLabels.map((_, i) => tkPalette[i % tkPalette.length]) 
            }] 
          },
          options: { 
            responsive: true, 
            plugins: { 
              legend: { position: 'bottom', labels: { font: { size: 10 }, boxWidth: 10, padding: 8 } }, 
              tooltip: { 
                callbacks: { 
                  label: function(ctx) { 
                    const total = ctx.dataset.data.reduce((a,b)=>a+b,0); 
                    const pct = total>0 ? Math.round(ctx.parsed/total*100) : 0; 
                    return ctx.parsed+' ('+pct+'%)'; 
                  } 
                } 
              }, 
              datalabels: { 
                color: '#fff', font: { size: 12, weight: 'bold' }, 
                formatter: v => v, 
                display: function(ctx) { return ctx.dataset.data[ctx.dataIndex] >= 2; } 
              } 
            } 
          }, 
          plugins: [ChartDataLabels]
        });
      }
    } else if (ourTkCtx) {
      ourTkCtx.parentElement.innerHTML = '<div class="alert alert-info mt-4 text-center"><strong>ТК не настроены</strong><br>Добавьте ваши ТК в файл <code>data/my_technical_committees.json</code><br>чтобы видеть статистику по интересующим комитетам.</div>';
      const breakdownParent = document.getElementById('ourTkBreakdownChart')?.parentElement;
      if (breakdownParent) breakdownParent.style.display = 'none';
    }

    // Круговая: Полимерные ГОСТы
    const polyTotal = POLYMER_STATS.total || 0;
    const polyCommented = POLYMER_STATS.commented || 0;
    const polyNoComment = polyTotal - polyCommented;
    const polymerCtx = document.getElementById('polymerChart');

    if (polyTotal > 0 && polymerCtx) {
      new Chart(polymerCtx, {
        type: 'doughnut', 
        data: { 
          labels: ['Комментарий направлен (' + polyCommented + ')', 'Без комментария (' + polyNoComment + ')'], 
          datasets: [{ 
            data: [polyCommented, polyNoComment], 
            backgroundColor: ['#2ecc71', '#e67e22'] 
          }] 
        },
        options: { 
          responsive: true, 
          plugins: { 
            legend: { position: 'bottom', labels: { font: { size: 10 }, boxWidth: 10, padding: 8 } }, 
            tooltip: { 
              callbacks: { 
                label: function(ctx) { 
                  const total = ctx.dataset.data.reduce((a,b)=>a+b,0); 
                  const pct = total>0 ? Math.round(ctx.parsed/total*100) : 0; 
                  return ctx.parsed+' ('+pct+'%)'; 
                } 
              } 
            }, 
            datalabels: { 
              color: '#fff', font: { size: 12, weight: 'bold' }, 
              formatter: function(value, ctx) { 
                const total = ctx.dataset.data.reduce((a,b)=>a+b,0); 
                const pct = total>0 ? Math.round(value/total*100) : 0; 
                return value+'\\n('+pct+'%)'; 
              }, 
              textAlign: 'center' 
            } 
          } 
        }, 
        plugins: [ChartDataLabels]
      });
    } else if (polymerCtx) {
      polymerCtx.parentElement.innerHTML = '<div class="alert alert-secondary mt-3 text-center" style="padding:30px;"><i style="font-size:2rem; color:#95a5a6;">📋</i><br><strong>Полимерных ГОСТов пока нет</strong><br><span class="text-muted">Данные появятся после обнаружения полимерных стандартов</span></div>';
    }
  }

  // Пересчёт колонок при переключении вкладок
  $('a[data-bs-toggle="tab"]').on('shown.bs.tab', function(e) { 
    $.fn.dataTable.tables({visible: true, api: true}).columns.adjust(); 
  });
  
  // Кнопка обновления
  document.getElementById('run-form')?.addEventListener('submit', () => {
    const btn = document.getElementById('run-btn');
    btn.disabled = true; 
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Обновление...';
  });
});
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────────────────────
# РОУТЫ
# ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"): 
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

@app.get("/health")
def health(): 
    return {"status": "ok"}

@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if not APP_PASSWORD: 
            err = "APP_PASSWORD не задан в переменных окружения"
        elif compare_digest(u, APP_USERNAME) and compare_digest(p, APP_PASSWORD):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else: 
            err = "Неверный логин или пароль"
    
    return render_template_string("""
    <!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Вход • Мониторинг Росстандарта</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body{background:linear-gradient(135deg,#008B92 0%,#01313D 100%);display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
        .card{background:#fff;padding:24px;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.2);width:100%;max-width:400px}
        .card h3{color:#01313D;margin-bottom:1.5rem}
        input{width:100%;padding:10px 12px;margin:8px 0 16px;border:1px solid #ddd;border-radius:8px;font-size:1rem}
        input:focus{outline:none;border-color:#008B92;box-shadow:0 0 0 3px rgba(0,139,146,.15)}
        button{width:100%;padding:12px;background:#008B92;color:#fff;border:0;border-radius:8px;font-weight:600;font-size:1rem;cursor:pointer;transition:background .2s}
        button:hover{background:#016b70}
        .err{color:#c0392b;font-size:0.9rem;margin-top:8px;background:#fff5f5;padding:8px 12px;border-radius:6px;border-left:3px solid #e74c3c}
        .logo{text-align:center;margin-bottom:1rem;font-size:2rem}
    </style></head>
    <body>
    <div class="card">
        <div class="logo">📋</div>
        <h3 class="text-center">Мониторинг Росстандарта</h3>
        <form method="post">
            <label class="form-label">Логин</label>
            <input name="username" required placeholder="polylab" autocomplete="username">
            <label class="form-label">Пароль</label>
            <input name="password" type="password" required placeholder="••••" autocomplete="current-password">
            <button type="submit">Войти</button>
        </form>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
    </div>
    </body></html>
    """, error=err)

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/")
def root():
    return redirect(url_for("dashboard") if session.get("authenticated") else url_for("login"))

@app.get("/dashboard")
@login_required
def dashboard():
    data = _prepare_dashboard_data()
    return render_template_string(
        DASHBOARD_TMPL,
        last_updated=data["updated"],
        stats=data["stats"],
        my_tks=data["my_tks"],
        gost_json=json.dumps(data["gost"], ensure_ascii=False),
        sp_json=json.dumps(data["sp"], ensure_ascii=False),
        my_tks_json=json.dumps(data["my_tks"], ensure_ascii=False),
        stats_json=json.dumps(data["stats"], ensure_ascii=False),
        polymer_json=json.dumps(data["polymer"], ensure_ascii=False),
        current_year=data["current_year"]
    )

@app.post("/run-now")
@login_required
def run_now():
    if not os.path.exists(MAIN_PATH):
        flash("❌ main.py не найден", "error")
        return redirect(url_for("dashboard"))
    try:
        result = subprocess.run(
            [sys.executable, MAIN_PATH], 
            cwd=os.path.dirname(MAIN_PATH), 
            capture_output=True, text=True, 
            timeout=RUN_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        flash("⏱ Таймаут обновления данных", "error")
        return redirect(url_for("dashboard"))
    except OSError as e:
        flash(f"⚠ Ошибка запуска: {e}", "error")
        return redirect(url_for("dashboard"))

    if result.returncode == 0:
        flash("✅ Данные успешно обновлены", "success")
    else:
        err = (result.stderr or result.stdout or "").strip()[:200] or f"код {result.returncode}"
        flash(f"❌ Ошибка: {err}", "error")
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=False)

# #!/usr/bin/env python3
# """Простой защищенный web-интерфейс для просмотра dashboard.html."""

# from __future__ import annotations

# import os
# import secrets
# import subprocess
# import sys
# from functools import wraps
# from hmac import compare_digest

# from flask import (
#     Flask,
#     flash,
#     redirect,
#     render_template_string,
#     request,
#     send_file,
#     session,
#     url_for,
# )


# from dotenv import load_dotenv


# load_dotenv()

# # После загрузки .env — путь к дашборду совпадает с generate_dashboard (DATA_DIR)
# from src.dashboard_config import DASHBOARD_OUTPUT_PATH

# APP_USERNAME = os.environ.get("APP_USERNAME", "polylab")
# APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY") or secrets.token_urlsafe(32)
# APP_PORT = int(os.environ.get("PORT", "8000"))
# APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
# DASHBOARD_PATH = DASHBOARD_OUTPUT_PATH
# MAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
# RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_NOW_TIMEOUT_SECONDS", "600"))

# app = Flask(__name__)
# app.secret_key = APP_SECRET_KEY


# LOGIN_TEMPLATE = """
# <!doctype html>
# <html lang="ru">
# <head>
#   <meta charset="utf-8">
#   <meta name="viewport" content="width=device-width, initial-scale=1">
#   <title>Вход в мониторинг</title>
#   <style>
#     body { font-family: Arial, sans-serif; background:#f4f6f8; margin:0; }
#     .card { max-width:420px; margin:80px auto; background:white; border-radius:12px;
#       padding:24px; box-shadow: 0 6px 24px rgba(0,0,0,0.12); }
#     h1 { margin-top:0; font-size:22px; color:#01313D; }
#     p { color:#5b6670; font-size:14px; }
#     label { display:block; margin-top:10px; font-size:13px; color:#01313D; }
#     input { width:100%; box-sizing:border-box; margin-top:4px; padding:10px;
#       border:1px solid #d9dee3; border-radius:8px; font-size:14px; }
#     button { width:100%; margin-top:16px; padding:10px; border:none; border-radius:8px;
#       background:#008B92; color:white; font-weight:700; cursor:pointer; }
#     .err { margin-top:12px; color:#c0392b; font-size:13px; }
#   </style>
# </head>
# <body>
#   <div class="card">
#     <h1>Мониторинг Росстандарта 2026</h1>
#     <p>Введите технический логин и пароль для доступа к дашборду.</p>
#     <form method="post" action="{{ url_for('login') }}">
#       <label for="username">Логин</label>
#       <input id="username" name="username" type="text" required autofocus />
#       <label for="password">Пароль</label>
#       <input id="password" name="password" type="password" required />
#       <button type="submit">Войти</button>
#     </form>
#     {% if error %}
#     <div class="err">{{ error }}</div>
#     {% endif %}
#   </div>
# </body>
# </html>
# """

# DASHBOARD_SHELL_TEMPLATE = """
# <!doctype html>
# <html lang="ru">
# <head>
#   <meta charset="utf-8">
#   <meta name="viewport" content="width=device-width, initial-scale=1">
#   <title>Мониторинг Росстандарта</title>
#   <style>
#     body { margin:0; font-family: Arial, sans-serif; background:#eef1f4; }
#     .topbar { display:flex; gap:12px; align-items:center; padding:12px 16px;
#       background: linear-gradient(135deg, #01313D 0%, #014D5A 100%); color:#fff; }
#     .topbar-title { margin-right:auto; font-weight:700; font-size:1.05rem; letter-spacing:0.02em; }
#     .btn { background:#008B92; color:#fff; border:0; border-radius:6px; padding:8px 12px;
#       cursor:pointer; font-weight:600; }
#     .btn[disabled] { opacity:0.75; cursor:progress; }
#     .btn.secondary { background:#4d5b66; text-decoration:none; display:inline-block; }
#     .msg { padding:8px 14px; font-size:13px; }
#     .ok { background:#e9f8ec; color:#1e7e34; }
#     .err { background:#fdebea; color:#b42318; }
#     iframe { display:block; width:100%; height:calc(100vh - 56px); border:0; background:white; }
#     .spinner {
#       width: 12px;
#       height: 12px;
#       border: 2px solid rgba(255,255,255,0.45);
#       border-top-color: #fff;
#       border-radius: 50%;
#       display: inline-block;
#       vertical-align: -2px;
#       margin-right: 6px;
#       animation: spin 0.9s linear infinite;
#     }
#     @keyframes spin { to { transform: rotate(360deg); } }
#   </style>
# </head>
# <body>
#   <div class="topbar">
#     <span class="topbar-title">Мониторинг Росстандарта 2026</span>
#     <form id="run-now-form" method="post" action="{{ url_for('run_now') }}" style="margin:0;">
#       <button id="run-now-btn" type="submit" class="btn">Обновить данные сейчас</button>
#     </form>
#     <a class="btn secondary" href="{{ url_for('logout') }}">Выйти</a>
#   </div>
#   {% with messages = get_flashed_messages(with_categories=true) %}
#     {% for category, text in messages %}
#       <div class="msg {{ 'ok' if category == 'success' else 'err' }}">{{ text }}</div>
#     {% endfor %}
#   {% endwith %}
#   <iframe src="{{ url_for('dashboard_raw') }}" title="Dashboard"></iframe>
#   <script>
#     const runNowForm = document.getElementById('run-now-form');
#     const runNowBtn = document.getElementById('run-now-btn');
#     if (runNowForm && runNowBtn) {
#       runNowForm.addEventListener('submit', function () {
#         runNowBtn.disabled = true;
#         runNowBtn.innerHTML = '<span class="spinner"></span>Идет обновление...';
#       });
#     }
#   </script>
# </body>
# </html>
# """


# def login_required(view):
#     @wraps(view)
#     def wrapped_view(*args, **kwargs):
#         if not session.get("authenticated"):
#             return redirect(url_for("login"))
#         return view(*args, **kwargs)

#     return wrapped_view


# @app.get("/health")
# def health():
#     return {"status": "ok"}


# @app.route("/login", methods=["GET", "POST"])
# def login():
#     error = None

#     if request.method == "POST":
#         username = request.form.get("username", "")
#         password = request.form.get("password", "")

#         if not APP_PASSWORD:
#             error = "APP_PASSWORD не задан. Укажите переменную окружения."
#         elif compare_digest(username, APP_USERNAME) and compare_digest(password, APP_PASSWORD):
#             session["authenticated"] = True
#             return redirect(url_for("dashboard"))
#         else:
#             error = "Неверный логин или пароль."

#     return render_template_string(LOGIN_TEMPLATE, error=error)


# @app.get("/logout")
# def logout():
#     session.clear()
#     return redirect(url_for("login"))


# @app.get("/")
# def root():
#     if session.get("authenticated"):
#         return redirect(url_for("dashboard"))
#     return redirect(url_for("login"))


# @app.get("/dashboard")
# @login_required
# def dashboard():
#     return render_template_string(DASHBOARD_SHELL_TEMPLATE)


# @app.get("/dashboard/raw")
# @login_required
# def dashboard_raw():
#     if not os.path.exists(DASHBOARD_PATH):
#         return (
#             "Файл dashboard.html пока не создан. Сначала запустите сбор данных (python main.py).",
#             404,
#         )
#     return send_file(DASHBOARD_PATH)


# @app.post("/run-now")
# @login_required
# def run_now():
#     if not os.path.exists(MAIN_PATH):
#         flash("Не найден main.py для запуска обновления.", "error")
#         return redirect(url_for("dashboard"))

#     try:
#         # Запускаем скрипт без аргументов — полная загрузка теперь по умолчанию
#         result = subprocess.run(
#             [sys.executable, MAIN_PATH],  # ← убран "--full-backfill"
#             cwd=os.path.dirname(MAIN_PATH),
#             capture_output=True,
#             text=True,
#             timeout=RUN_TIMEOUT_SECONDS,
#             check=False,
#         )
#     except subprocess.TimeoutExpired:
#         flash("Обновление не завершилось по таймауту.", "error")
#         return redirect(url_for("dashboard"))
#     except OSError as exc:
#         flash(f"Ошибка запуска обновления: {exc}", "error")
#         return redirect(url_for("dashboard"))

#     if result.returncode == 0:
#         flash("Данные успешно обновлены. Дашборд перезагружен.", "success")
#     else:
#         stderr = (result.stderr or "").strip()
#         detail = stderr[:180] if stderr else f"код {result.returncode}"
#         flash(f"Обновление завершилось с ошибкой: {detail}", "error")
    
#     return redirect(url_for("dashboard"))


# if __name__ == "__main__":
#     app.run(host=APP_HOST, port=APP_PORT, debug=False)
