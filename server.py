"""
Калькулятор себестоимости — сервер.

Отдаёт калькулятор по публичной ссылке и собирает расчёты в SQLite.
Админ-режим («Все расчёты») закрыт кодом из переменной окружения ADMIN_CODE.

Запуск локально:  uvicorn server:app --reload
На Railway:       Procfile
"""

import csv
import io
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

BASE = Path(__file__).parent

# На Railway диск (Volume) монтируется в /data — там база переживает передеплой.
# Локально — файл рядом со скриптом.
DB_PATH = os.environ.get("DB_PATH") or (
    "/data/sebes.db" if Path("/data").is_dir() else str(BASE / "sebes.db")
)

ADMIN_CODE = os.environ.get("ADMIN_CODE", "")

MAX_BODY = 400 * 1024      # 400 КБ на один расчёт (фото сжимается до ~30 КБ)
MAX_ROWS = 20000           # потолок, чтобы публичная ссылка не забила диск
RATE_LIMIT = 30            # расчётов с одного IP за окно
RATE_WINDOW = 60           # секунд

app = FastAPI(title="Себестоимость из Китая", docs_url=None, redoc_url=None)

_hits: dict[str, list[float]] = {}

FIELDS = [
    "name", "priceCny", "perBox", "cbm", "weight",
    "rateCny", "freight", "mode", "customsUnit", "customsKg",
    "rateUzs", "extraUzs",
]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                first_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calc (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id  TEXT NOT NULL,
                created_at TEXT NOT NULL,
                name       TEXT,
                priceCny   TEXT, perBox   TEXT, cbm         TEXT, weight     TEXT,
                rateCny    TEXT, freight  TEXT, mode        TEXT,
                customsUnit TEXT, customsKg TEXT,
                rateUzs    TEXT, extraUzs TEXT,
                total_usd  REAL, total_uzs REAL,
                photo      TEXT
            );
            CREATE INDEX IF NOT EXISTS calc_created ON calc(created_at DESC);
            """
        )
        conn.commit()


init_db()


def num(v) -> float:
    """'12,4' / ' 12.4 ' -> 12.4 ; мусор -> 0.0"""
    try:
        s = str(v if v is not None else "").replace(" ", "").replace(" ", "").replace(",", ".")
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def compute(d: dict) -> tuple[float, float]:
    """Та же формула, что в браузере. Считаем на сервере, чтобы в отчёте
    нельзя было подсунуть произвольный итог."""
    per_box = num(d.get("perBox"))
    rate_cny = num(d.get("rateCny"))

    goods = num(d.get("priceCny")) / rate_cny if rate_cny > 0 else 0.0
    freight = num(d.get("cbm")) * num(d.get("freight")) / per_box if per_box > 0 else 0.0
    if d.get("mode") == "kg":
        customs = num(d.get("weight")) * num(d.get("customsKg")) / per_box if per_box > 0 else 0.0
    else:
        customs = num(d.get("customsUnit"))

    total_usd = goods + freight + customs
    total_uzs = total_usd * num(d.get("rateUzs")) + num(d.get("extraUzs"))
    return total_usd, total_uzs


def rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        _hits[ip] = hits
        return False
    hits.append(now)
    _hits[ip] = hits
    return True


def check_admin(code: str):
    if not ADMIN_CODE:
        raise HTTPException(503, "ADMIN_CODE не задан в переменных окружения")
    if code != ADMIN_CODE:
        raise HTTPException(403, "Неверный код")


def device_label(conn, device_id: str) -> str:
    row = conn.execute("SELECT label FROM device WHERE id = ?", (device_id,)).fetchone()
    if row:
        return row["label"]
    n = conn.execute("SELECT COUNT(*) AS c FROM device").fetchone()["c"] + 1
    label = f"Телефон {n}"
    conn.execute(
        "INSERT INTO device (id, label, first_seen) VALUES (?, ?, ?)",
        (device_id, label, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    return label


# ---------------------------------------------------------------- страницы

def page(filename: str) -> HTMLResponse:
    f = BASE / filename
    if not f.exists():
        raise HTTPException(500, f"{filename} не найден рядом с server.py")
    return HTMLResponse(f.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def index():
    return page("index.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return page("admin.html")


# ---------------------------------------------------------------- приём расчётов

@app.post("/api/calc")
async def save_calc(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Слишком большой расчёт")

    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    if not rate_ok(ip):
        raise HTTPException(429, "Слишком часто — подождите минуту")

    try:
        d = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON")
    if not isinstance(d, dict):
        raise HTTPException(400, "Ожидался объект")

    device_id = str(d.get("device") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9\-]{8,64}", device_id):
        raise HTTPException(400, "Некорректный идентификатор устройства")

    photo = d.get("photo") or ""
    if photo and not photo.startswith("data:image/"):
        photo = ""

    total_usd, total_uzs = compute(d)

    with closing(db()) as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM calc").fetchone()["c"]
        if total >= MAX_ROWS:
            raise HTTPException(507, "Хранилище заполнено — выгрузите и очистите старые расчёты")

        device_label(conn, device_id)
        values = [str(d.get(f) or "")[:300] for f in FIELDS]
        conn.execute(
            f"""INSERT INTO calc (device_id, created_at, {', '.join(FIELDS)},
                                  total_usd, total_uzs, photo)
                VALUES (?, ?, {', '.join('?' * len(FIELDS))}, ?, ?, ?)""",
            [device_id, datetime.now(timezone.utc).isoformat(timespec="seconds")]
            + values + [total_usd, total_uzs, photo],
        )
        conn.commit()

    return JSONResponse({"ok": True, "total_usd": total_usd, "total_uzs": total_uzs})


# ---------------------------------------------------------------- админ

@app.get("/api/admin/list")
def admin_list(code: str = Query("")):
    check_admin(code)
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT c.*, COALESCE(d.label, '—') AS device_label
               FROM calc c LEFT JOIN device d ON d.id = c.device_id
               ORDER BY c.id DESC LIMIT 2000"""
        ).fetchall()
        devices = conn.execute("SELECT id, label FROM device ORDER BY label").fetchall()
    return {
        "items": [dict(r) for r in rows],
        "devices": [dict(r) for r in devices],
    }


@app.post("/api/admin/rename")
async def admin_rename(request: Request, code: str = Query("")):
    check_admin(code)
    d = await request.json()
    device_id, label = str(d.get("id", "")), str(d.get("label", "")).strip()[:60]
    if not device_id or not label:
        raise HTTPException(400, "Нужны id и новое название")
    with closing(db()) as conn:
        conn.execute("UPDATE device SET label = ? WHERE id = ?", (label, device_id))
        conn.commit()
    return {"ok": True}


@app.delete("/api/admin/calc/{calc_id}")
def admin_delete(calc_id: int, code: str = Query("")):
    check_admin(code)
    with closing(db()) as conn:
        conn.execute("DELETE FROM calc WHERE id = ?", (calc_id,))
        conn.commit()
    return {"ok": True}


@app.get("/api/admin/export.csv")
def admin_export(code: str = Query("")):
    check_admin(code)
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT c.*, COALESCE(d.label, '—') AS device_label
               FROM calc c LEFT JOIN device d ON d.id = c.device_id
               ORDER BY c.id DESC"""
        ).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([
        "Кто считал", "Когда", "Товар", "Цена ¥/шт", "Кол-во в коробке", "СВМ м³",
        "Вес кг", "Курс ¥/$", "Доставка $/м³", "Растаможка", "Курс сум/$",
        "Доп. расходы сум/шт", "Себестоимость $/шт", "Себестоимость сум/шт",
    ])
    for r in rows:
        customs = (f"{r['customsKg']} $/кг" if r["mode"] == "kg" else f"{r['customsUnit']} $/шт")
        w.writerow([
            r["device_label"], r["created_at"], r["name"], r["priceCny"], r["perBox"],
            r["cbm"], r["weight"], r["rateCny"], r["freight"], customs, r["rateUzs"],
            r["extraUzs"],
            f"{r['total_usd']:.2f}".replace(".", ","),
            str(round(r["total_uzs"] or 0)),
        ])

    data = "﻿" + buf.getvalue()          # BOM, чтобы Excel не ломал кириллицу
    return StreamingResponse(
        io.BytesIO(data.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sebestoimost_vse.csv"'},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": DB_PATH}
