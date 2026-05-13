from __future__ import annotations

import os
import ssl
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from datetime import datetime

from src.config import SMTP_HOST, SMTP_PORT, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAILS

logger = logging.getLogger(__name__)


_DASHBOARD_IMG_BLOCK = """
<div style="margin:20px auto;max-width:1000px;text-align:center;">
    <div style="background:#01313D;color:#fff;padding:12px 20px;border-radius:8px 8px 0 0;font-size:14px;font-weight:bold;">
        📊 Текущее состояние дашборда
    </div>
    <div style="border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;overflow:hidden;">
        <img src="cid:dashboard-screenshot" style="width:100%;display:block;" alt="Дашборд мониторинга Росстандарта">
    </div>
</div>
"""


def _send_email(subject: str, html_body: str, image_path: str | None = None) -> bool:
    """Отправляет email через Gmail SMTP.

    Args:
        subject: тема письма.
        html_body: HTML-тело письма.
        image_path: путь к PNG-скриншоту дашборда (встраивается inline).
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.error(
            "GMAIL_ADDRESS и GMAIL_APP_PASSWORD должны быть заданы "
            "в переменных окружения."
        )
        return False

    recipients = RECIPIENT_EMAILS if isinstance(RECIPIENT_EMAILS, list) else [RECIPIENT_EMAILS]

    # Если есть скриншот — вставляем его inline через CID
    attach_image = image_path and os.path.exists(image_path)

    if attach_image:
        # Добавляем блок с изображением перед закрывающими тегами
        html_body = html_body.replace("</body>", _DASHBOARD_IMG_BLOCK + "</body>")

        # MIMEMultipart("related") позволяет ссылаться на вложения по CID
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ", ".join(recipients)

        # HTML-часть
        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt_part)

        # PNG-изображение
        with open(image_path, "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", "<dashboard-screenshot>")
        img.add_header("Content-Disposition", "inline", filename="dashboard.png")
        msg.attach(img)
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30, context=context) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info(f"Отчёт отправлен на {', '.join(recipients)}")
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.error(f"Ошибка отправки email: {e}")
        return False


def send_no_changes_email(
    sp_error: bool = False,
    gost_error: bool = False,
    image_path: str | None = None,
) -> bool:
    """Отправляет email о том, что новых уведомлений не выявлено."""
    today = datetime.now().strftime("%d.%m.%Y")
    subject = f"Росстандарт: новых уведомлений нет ({today})"

    # Предупреждения об ошибках (если были)
    warnings_html = ""
    if sp_error or gost_error:
        parts = []
        if sp_error:
            parts.append("СП (rst.gov.ru)")
        if gost_error:
            parts.append("ГОСТы (fgis.gost.ru)")
        warnings_html = f"""
        <div style="background:#fef9e7;border-left:4px solid #f39c12;padding:12px 16px;margin:16px 24px;border-radius:4px;">
            <strong style="color:#e67e22;">⚠ Внимание:</strong>
            <span style="color:#7d6608;">Не удалось проверить: {', '.join(parts)}.
            Сайт мог быть недоступен.</span>
        </div>"""

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;margin:0;padding:20px;background:#f5f5f5;">
    <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#27ae60;color:#fff;padding:20px 24px;">
            <h1 style="margin:0;font-size:20px;">Мониторинг Росстандарта</h1>
            <p style="margin:4px 0 0;opacity:0.8;font-size:14px;">Отчёт от {today}</p>
        </div>
        <div style="padding:24px;text-align:center;">
            <div style="font-size:48px;margin-bottom:12px;">✅</div>
            <h2 style="color:#2c3e50;margin:0 0 8px;">Новых уведомлений не выявлено</h2>
            <p style="color:#7f8c8d;font-size:14px;margin:0;">
                Проверены уведомления о сводах правил (СП) и публичных обсуждениях ГОСТов.
                <br>Изменений с момента последней проверки не обнаружено.
            </p>
        </div>
        {warnings_html}
        <div style="padding:16px 24px;background:#ecf0f1;font-size:12px;color:#666;">
            Автоматический отчёт &mdash; ежедневная проверка
        </div>
    </div>
</body>
</html>"""

    return _send_email(subject, html_body, image_path=image_path)


def send_email_report(
    notifications: list[dict],
    image_path: str | None = None,
) -> bool:
    """Отправляет email-отчёт о новых уведомлениях СП."""
    if not notifications:
        logger.info("Нет новых уведомлений для отправки.")
        return True

    subject = (
        f"Росстандарт: {len(notifications)} новых уведомлений "
        f"({datetime.now().strftime('%d.%m.%Y')})"
    )
    html_body = _build_html_report(notifications)
    return _send_email(subject, html_body, image_path=image_path)


def _build_html_report(notifications: list[dict]) -> str:
    """Формирует HTML-тело email-отчёта."""
    rows = ""
    for i, n in enumerate(notifications, 1):
        notification_type = n.get("notification_type", "Не определён")
        if "завершении" in notification_type.lower():
            type_badge = (
                '<span style="background:#e74c3c;color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:12px;">Завершение обсуждения</span>'
            )
        elif "разработке" in notification_type.lower():
            type_badge = (
                '<span style="background:#3498db;color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:12px;">Разработка проекта</span>'
            )
        else:
            type_badge = (
                '<span style="background:#95a5a6;color:#fff;padding:2px 8px;'
                'border-radius:3px;font-size:12px;">Свод правил</span>'
            )

        doc_type = n.get("doc_type", "Свод правил")
        project_name = n.get("project_name", n.get("title", "Без названия"))
        developer = n.get("developer", "Не указан")
        placement_date = n.get("placement_date", n.get("date", "—"))
        stakeholders = n.get("stakeholders", [])
        stakeholders_text = ", ".join(stakeholders) if stakeholders else "—"
        url = n.get("url", "#")

        attachments = n.get("attachments", [])
        attachments_html = ""
        if attachments:
            att_items = "".join(
                f'<li><a href="{a["url"]}" style="color:#3498db;">'
                f'{a["name"]}</a></li>'
                for a in attachments
            )
            attachments_html = f"<ul style='margin:4px 0;padding-left:20px;'>{att_items}</ul>"

        rows += f"""
        <tr style="border-bottom:1px solid #e0e0e0;">
            <td style="padding:12px 8px;text-align:center;color:#666;">{i}</td>
            <td style="padding:12px 8px;">
                {type_badge}<br>
                <small style="color:#888;">{doc_type}</small>
            </td>
            <td style="padding:12px 8px;">
                <a href="{url}" style="color:#2c3e50;text-decoration:none;font-weight:bold;">
                    {project_name}
                </a>
                {attachments_html}
            </td>
            <td style="padding:12px 8px;">{developer}</td>
            <td style="padding:12px 8px;text-align:center;white-space:nowrap;">{placement_date}</td>
            <td style="padding:12px 8px;font-size:13px;">{stakeholders_text}</td>
        </tr>"""

    today = datetime.now().strftime("%d.%m.%Y")
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;margin:0;padding:20px;background:#f5f5f5;">
    <div style="max-width:900px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#2c3e50;color:#fff;padding:20px 24px;">
            <h1 style="margin:0;font-size:20px;">Новые уведомления Росстандарта</h1>
            <p style="margin:4px 0 0;opacity:0.8;font-size:14px;">
                Отчёт от {today} &mdash; найдено {len(notifications)} новых уведомлений
            </p>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            <thead>
                <tr style="background:#ecf0f1;">
                    <th style="padding:10px 8px;text-align:center;width:30px;">#</th>
                    <th style="padding:10px 8px;text-align:left;">Тип</th>
                    <th style="padding:10px 8px;text-align:left;">Название проекта</th>
                    <th style="padding:10px 8px;text-align:left;">Разработчик</th>
                    <th style="padding:10px 8px;text-align:center;">Дата</th>
                    <th style="padding:10px 8px;text-align:left;">Заинтересованные лица</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
        <div style="padding:16px 24px;background:#ecf0f1;font-size:12px;color:#666;">
            Источник: <a href="https://www.rst.gov.ru/portal/gost/home/activity/standardization/notification/notificationssetrules" style="color:#3498db;">rst.gov.ru</a>
            &mdash; автоматический отчёт
        </div>
    </div>
</body>
</html>"""
    return html
