from __future__ import annotations

import logging
from datetime import datetime

from src.notifier import _send_email
from src.gost_config import GOST_PAGE_URL

logger = logging.getLogger(__name__)


def send_gost_email_report(
    notifications: list[dict],
    image_path: str | None = None,
) -> bool:
    """Отправляет email-отчёт о новых уведомлениях по ГОСТам."""
    if not notifications:
        logger.info("ГОСТ: нет новых уведомлений для отправки.")
        return True

    subject = (
        f"Росстандарт — ГОСТы: {len(notifications)} новых публичных обсуждений "
        f"({datetime.now().strftime('%d.%m.%Y')})"
    )
    html_body = _build_gost_html_report(notifications)
    return _send_email(subject, html_body, image_path=image_path)


def _build_gost_html_report(notifications: list[dict]) -> str:
    """Формирует HTML-тело email-отчёта о ГОСТах."""
    rows = ""
    for i, n in enumerate(notifications, 1):
        doc_type = n.get("doc_type", "ГОСТ")
        project_name = n.get("project_name", "Без названия")
        tk = n.get("technical_committee", "—")
        developer = n.get("developer", "—")
        start_date = n.get("start_date", "—")
        end_date = n.get("end_date", "")
        status = n.get("status", "")
        url = n.get("url", "#")
        prns_code = n.get("prns_code", "")

        # Бейдж типа документа
        if "ПНСТ" in doc_type:
            type_badge = (
                '<span style="background:#e67e22;color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:12px;">ПНСТ</span>'
            )
        elif "ГОСТ Р" in doc_type:
            type_badge = (
                '<span style="background:#2980b9;color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:12px;">ГОСТ Р</span>'
            )
        else:
            type_badge = (
                '<span style="background:#27ae60;color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:12px;">ГОСТ</span>'
            )

        # Дата завершения обсуждения (если есть)
        date_info = start_date
        if end_date:
            date_info += f'<br><small style="color:#e74c3c;">завершение: {end_date}</small>'

        rows += f"""
        <tr style="border-bottom:1px solid #e0e0e0;">
            <td style="padding:12px 8px;text-align:center;color:#666;">{i}</td>
            <td style="padding:12px 8px;">
                {type_badge}
                <br><small style="color:#888;">{prns_code}</small>
            </td>
            <td style="padding:12px 8px;">
                <a href="{url}" style="color:#1a5276;text-decoration:none;font-weight:bold;">
                    {project_name}
                </a>
            </td>
            <td style="padding:12px 8px;font-size:13px;font-weight:bold;color:#2c3e50;">
                {tk}
            </td>
            <td style="padding:12px 8px;font-size:13px;">{developer}</td>
            <td style="padding:12px 8px;text-align:center;white-space:nowrap;">
                {date_info}
            </td>
        </tr>"""

    today = datetime.now().strftime("%d.%m.%Y")
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;margin:0;padding:20px;background:#f5f5f5;">
    <div style="max-width:1000px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#1a5276;color:#fff;padding:20px 24px;">
            <h1 style="margin:0;font-size:20px;">Новые публичные обсуждения ГОСТов</h1>
            <p style="margin:4px 0 0;opacity:0.8;font-size:14px;">
                Отчёт от {today} &mdash; найдено {len(notifications)} новых уведомлений
            </p>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            <thead>
                <tr style="background:#d6eaf8;">
                    <th style="padding:10px 8px;text-align:center;width:30px;">#</th>
                    <th style="padding:10px 8px;text-align:left;">Тип</th>
                    <th style="padding:10px 8px;text-align:left;">Наименование проекта стандарта</th>
                    <th style="padding:10px 8px;text-align:left;">Технический комитет</th>
                    <th style="padding:10px 8px;text-align:left;">Разработчик</th>
                    <th style="padding:10px 8px;text-align:center;">Дата</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
        <div style="padding:16px 24px;background:#d6eaf8;font-size:12px;color:#666;">
            Источник: <a href="{GOST_PAGE_URL}" style="color:#1a5276;">ФГИС Росстандарта</a>
            &mdash; автоматический отчёт
        </div>
    </div>
</body>
</html>"""
    return html
