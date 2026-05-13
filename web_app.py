#!/usr/bin/env python3
"""Простой защищенный web-интерфейс для просмотра dashboard.html."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from functools import wraps
from hmac import compare_digest

from flask import (
    Flask,
    flash,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)


def _load_local_env() -> None:
    """Подгружает .env для локального запуска (без перезаписи существующих env)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw in env_file:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


_load_local_env()

# После загрузки .env — путь к дашборду совпадает с generate_dashboard (DATA_DIR)
from src.dashboard_config import DASHBOARD_OUTPUT_PATH

APP_USERNAME = os.environ.get("APP_USERNAME", "polylab")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY") or secrets.token_urlsafe(32)
APP_PORT = int(os.environ.get("PORT", "8000"))
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
DASHBOARD_PATH = DASHBOARD_OUTPUT_PATH
MAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_NOW_TIMEOUT_SECONDS", "600"))

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход в мониторинг</title>
  <style>
    body { font-family: Arial, sans-serif; background:#f4f6f8; margin:0; }
    .card { max-width:420px; margin:80px auto; background:white; border-radius:12px;
      padding:24px; box-shadow: 0 6px 24px rgba(0,0,0,0.12); }
    h1 { margin-top:0; font-size:22px; color:#01313D; }
    p { color:#5b6670; font-size:14px; }
    label { display:block; margin-top:10px; font-size:13px; color:#01313D; }
    input { width:100%; box-sizing:border-box; margin-top:4px; padding:10px;
      border:1px solid #d9dee3; border-radius:8px; font-size:14px; }
    button { width:100%; margin-top:16px; padding:10px; border:none; border-radius:8px;
      background:#008B92; color:white; font-weight:700; cursor:pointer; }
    .err { margin-top:12px; color:#c0392b; font-size:13px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Мониторинг Росстандарта 2026</h1>
    <p>Введите технический логин и пароль для доступа к дашборду.</p>
    <form method="post" action="{{ url_for('login') }}">
      <label for="username">Логин</label>
      <input id="username" name="username" type="text" required autofocus />
      <label for="password">Пароль</label>
      <input id="password" name="password" type="password" required />
      <button type="submit">Войти</button>
    </form>
    {% if error %}
    <div class="err">{{ error }}</div>
    {% endif %}
  </div>
</body>
</html>
"""

DASHBOARD_SHELL_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Мониторинг Росстандарта</title>
  <style>
    body { margin:0; font-family: Arial, sans-serif; background:#eef1f4; }
    .topbar { display:flex; gap:12px; align-items:center; padding:12px 16px;
      background: linear-gradient(135deg, #01313D 0%, #014D5A 100%); color:#fff; }
    .topbar-title { margin-right:auto; font-weight:700; font-size:1.05rem; letter-spacing:0.02em; }
    .btn { background:#008B92; color:#fff; border:0; border-radius:6px; padding:8px 12px;
      cursor:pointer; font-weight:600; }
    .btn[disabled] { opacity:0.75; cursor:progress; }
    .btn.secondary { background:#4d5b66; text-decoration:none; display:inline-block; }
    .msg { padding:8px 14px; font-size:13px; }
    .ok { background:#e9f8ec; color:#1e7e34; }
    .err { background:#fdebea; color:#b42318; }
    iframe { display:block; width:100%; height:calc(100vh - 56px); border:0; background:white; }
    .spinner {
      width: 12px;
      height: 12px;
      border: 2px solid rgba(255,255,255,0.45);
      border-top-color: #fff;
      border-radius: 50%;
      display: inline-block;
      vertical-align: -2px;
      margin-right: 6px;
      animation: spin 0.9s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="topbar">
    <span class="topbar-title">Мониторинг Росстандарта 2026</span>
    <form id="run-now-form" method="post" action="{{ url_for('run_now') }}" style="margin:0;">
      <button id="run-now-btn" type="submit" class="btn">Обновить данные сейчас</button>
    </form>
    <a class="btn secondary" href="{{ url_for('logout') }}">Выйти</a>
  </div>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, text in messages %}
      <div class="msg {{ 'ok' if category == 'success' else 'err' }}">{{ text }}</div>
    {% endfor %}
  {% endwith %}
  <iframe src="{{ url_for('dashboard_raw') }}" title="Dashboard"></iframe>
  <script>
    const runNowForm = document.getElementById('run-now-form');
    const runNowBtn = document.getElementById('run-now-btn');
    if (runNowForm && runNowBtn) {
      runNowForm.addEventListener('submit', function () {
        runNowBtn.disabled = true;
        runNowBtn.innerHTML = '<span class="spinner"></span>Идет обновление...';
      });
    }
  </script>
</body>
</html>
"""


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


@app.get("/health")
def health():
    return {"status": "ok"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if not APP_PASSWORD:
            error = "APP_PASSWORD не задан. Укажите переменную окружения."
        elif compare_digest(username, APP_USERNAME) and compare_digest(password, APP_PASSWORD):
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        else:
            error = "Неверный логин или пароль."

    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def root():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    return render_template_string(DASHBOARD_SHELL_TEMPLATE)


@app.get("/dashboard/raw")
@login_required
def dashboard_raw():
    if not os.path.exists(DASHBOARD_PATH):
        return (
            "Файл dashboard.html пока не создан. Сначала запустите сбор данных (python main.py).",
            404,
        )
    return send_file(DASHBOARD_PATH)


@app.post("/run-now")
@login_required
def run_now():
    if not os.path.exists(MAIN_PATH):
        flash("Не найден main.py для запуска обновления.", "error")
        return redirect(url_for("dashboard"))

    try:
        result = subprocess.run(
            [sys.executable, MAIN_PATH],
            cwd=os.path.dirname(MAIN_PATH),
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        flash("Обновление не завершилось по таймауту.", "error")
        return redirect(url_for("dashboard"))
    except OSError as exc:
        flash(f"Ошибка запуска обновления: {exc}", "error")
        return redirect(url_for("dashboard"))

    combined_out = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode == 0 and "RUN_LOCKED" in combined_out:
        flash("Обновление уже выполняется в другом процессе. Попробуйте позже.", "error")
    elif result.returncode == 0:
        flash("Данные успешно обновлены. Дашборд перезагружен.", "success")
    else:
        stderr = (result.stderr or "").strip()
        detail = stderr[:180] if stderr else f"код {result.returncode}"
        flash(f"Обновление завершилось с ошибкой: {detail}", "error")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=False)
