#!/usr/bin/env python3
"""
Hikvision NVR/DVR -> Gmail -> FTP.

Забирает непрочитанные письма от регистратора Hikvision из ящика Gmail (IMAP),
вытаскивает фото из вложений и заливает их на FTP.
После успешной загрузки письмо помечается прочитанным и получает метку Gmail
(по умолчанию "hik-processed"), а при delete_after_upload = true — уходит
в Корзину Gmail. Если загрузка не удалась — письмо остаётся
непрочитанным и будет обработано при следующем запуске.

Дополнительно (analyze.py): детектор находит людей, машины, собак и кошек на
фото из писем и на новых фото другого регистратора в папках IPCAM_HOME* на FTP,
а после прогона на почту уходит отчёт с текстом от Claude.

Почта и FTP — только стандартная библиотека Python 3.8+; анализу нужен requirements.txt.

Запуск:
    python hik_mail2ftp.py              # один проход
    python hik_mail2ftp.py --loop       # бесконечно, с интервалом из конфига
    python hik_mail2ftp.py --dry-run    # ничего не заливать и не помечать, только лог
"""

import argparse
import configparser
import email
import email.policy
import ftplib
import imaplib
import io
import json
import logging
import os
import queue
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger("hik_mail2ftp")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp")

# Типичное тело письма Hikvision:
#   EVENT TYPE:        Motion Detected
#   EVENT TIME:        2026-10-01,12:34:56
#   NVR NAME:          Embedded Net DVR
#   CAMERA NAME(NUM):  Camera 01(D1)
RE_EVENT_TYPE = re.compile(r"EVENT\s*TYPE\s*:\s*(.+)", re.I)
RE_EVENT_TIME = re.compile(r"EVENT\s*TIME\s*:\s*(\d{4}-\d{2}-\d{2})[,\sT]+(\d{2}:\d{2}:\d{2})", re.I)
RE_CAMERA = re.compile(r"CAMERA\s*NAME(?:\s*\(NUM\))?\s*:\s*(.+)", re.I)
RE_DEVICE = re.compile(r"(?:NVR|DVR|DEVICE)\s*NAME\s*:\s*(.+)", re.I)
# Все "Имя(Dn)" в блоке камер — письмо может быть сразу по нескольким каналам:
#   "Motion Detected On Channel D1 D4"
RE_CAM_ALL = re.compile(r"([^\n(),:]+?)\s*\(\s*(D\d+)\s*\)", re.I)
# Канал из имени вложения: "D04-1.jpg" -> D4
RE_IMG_CH = re.compile(r"^D0*(\d+)[-_]", re.I)
# Папка-дата другого регистратора: "2026_10_04-2026_10_04" (последняя дата — конец)
RE_DATE_DIR = re.compile(r"(\d{4})_(\d{2})_(\d{2})(?:-(\d{4})_(\d{2})_(\d{2}))?")


def ftp_time(s):
    """Время FTP (UTC, YYYYMMDDHHMMSS) -> локальное без tzinfo."""
    if not s:
        return None
    try:
        t = datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return t.astimezone().replace(tzinfo=None)


# ---------------------------------------------------------------- config ---

def load_config(path):
    """Настройки из config.ini; любую можно переопределить переменной окружения
    SECTION_KEY (IMAP_PASSWORD, FTP_HOST, FTP_USER, ...). Файл необязателен."""
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")

    def get(section, key, fallback=None, required=False):
        val = os.environ.get(f"{section.upper()}_{key.upper()}")
        if val in (None, ""):
            val = cfg.get(section, key, fallback=fallback)
        if required and not (val or "").strip():
            sys.exit(f"Не задан параметр [{section}] {key} (или переменная {section.upper()}_{key.upper()})")
        return val.strip() if isinstance(val, str) else val

    def get_bool(section, key, fallback):
        return str(get(section, key, str(fallback))).lower() in ("1", "true", "yes", "on")

    return {
        "imap_host": get("imap", "host", "imap.gmail.com"),
        "imap_port": int(get("imap", "port", "993")),
        "imap_user": get("imap", "user", required=True),
        "imap_password": get("imap", "password", required=True),
        "imap_folder": get("imap", "folder", "INBOX"),
        "imap_from": get("imap", "from_filter", ""),
        "imap_subject": get("imap", "subject_filter", ""),
        "done_label": get("imap", "processed_label", "hik-processed"),
        "delete_after": get_bool("imap", "delete_after_upload", False),
        "ftp_host": get("ftp", "host", required=True),
        "ftp_port": int(get("ftp", "port", "21")),
        "ftp_user": get("ftp", "user", required=True),
        "ftp_password": get("ftp", "password", required=True),
        "ftp_tls": get_bool("ftp", "tls", False),
        "ftp_passive": get_bool("ftp", "passive", True),
        "ftp_root": get("ftp", "root", "/") or "/",
        "ftp_connections": int(get("ftp", "connections", "4")),
        "path_template": get("ftp", "path_template", "{camera}/{date}/{time}_{filename}"),
        "interval": int(get("main", "interval_seconds", "60")),
        "max_per_run": int(get("main", "max_messages_per_run", "200")),
        "batch_size": max(1, int(get("main", "batch_size", "20"))),
        "log_file": get("main", "log_file", ""),
        "analyze": get_bool("analyze", "enabled", False),
        "yolo_model": get("analyze", "model", "yolo11s.pt"),
        "confidence": float(get("analyze", "confidence", "0.4")),
        "visit_gap": int(get("analyze", "visit_gap_minutes", "5")),
        "journal_dir": get("analyze", "journal_dir", "/hikvision/reports"),
        "report": get_bool("report", "enabled", False),
        "report_to": get("report", "to", ""),
        "claude_model": get("report", "claude_model", "claude-opus-5-5"),
        "max_images": int(get("report", "max_images", "6")),
        "send_empty": get_bool("report", "send_empty", False),
        "pets": get("report", "pets", ""),
        "smtp_host": get("report", "smtp_host", "smtp.gmail.com"),
        "smtp_port": int(get("report", "smtp_port", "465")),
        "ipcam_dirs": [d.strip() for d in get("ipcam", "dirs", "").split(",") if d.strip()],
        "ipcam_lookback": float(get("ipcam", "first_run_lookback_hours", "6")),
        "ipcam_max": int(get("ipcam", "max_files_per_run", "3000")),
        "cleanup_dirs": [d.strip() for d in get("ipcam", "cleanup_dirs", "").split(",") if d.strip()],
        "quarantine_suffix": get("ipcam", "quarantine_suffix", "_empty"),
        "quarantine_days": float(get("ipcam", "quarantine_days", "3")),
        # разово (ручной запуск): заново пройти папки чистки начиная с этой даты
        "ipcam_since": get("ipcam", "since", ""),
    }


# ----------------------------------------------------------------- parse ---

def safe_name(s, default="unknown"):
    s = (s or "").strip()
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s)
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:80] or default


def message_text(msg):
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() == "text" and not part.get_filename():
            try:
                txt = part.get_content()
            except Exception:
                txt = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            if part.get_content_subtype() == "html":
                txt = re.sub(r"<br\s*/?>|</p>|</div>", "\n", txt, flags=re.I)
                txt = re.sub(r"<[^>]+>", "", txt)
            parts.append(txt)
    return "\n".join(parts)


def parse_event(msg):
    """Метаданные события из тела/темы письма Hikvision."""
    text = message_text(msg)
    subject = str(msg.get("Subject", ""))

    def first(rx, src):
        m = rx.search(src)
        return m.group(1).strip() if m else ""

    camera = first(RE_CAMERA, text)
    # "Camera 01(D1)" -> оставляем как есть, safe_name уберёт лишнее
    ev_time = None
    m = RE_EVENT_TIME.search(text)
    if m:
        try:
            ev_time = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if ev_time is None:
        try:
            ev_time = parsedate_to_datetime(msg["Date"]).astimezone().replace(tzinfo=None)
        except Exception:
            ev_time = datetime.now()

    # Канал: "Camera 02(D2)" или тема "... On Channel D2"
    channel = first(re.compile(r"\((D?\d+)\)\s*$"), camera) or \
        first(re.compile(r"Channel\s+(\S+)", re.I), subject)

    # Карта канал -> "Camera 04(D4)" по всем камерам из письма
    cameras = {}
    i = text.upper().find("CAMERA NAME")
    if i >= 0:
        for name, ch in RE_CAM_ALL.findall(text[i:]):
            key = "D%d" % int(ch[1:])
            cameras.setdefault(key, f"{name.strip()}({ch})")

    return {
        "camera": safe_name(camera or first(RE_CAMERA, subject), "camera"),
        "channel": safe_name(channel, "ch"),
        "cameras": cameras,
        "event": safe_name(first(RE_EVENT_TYPE, text) or subject, "event"),
        "device": safe_name(first(RE_DEVICE, text), "nvr"),
        "time": ev_time,
    }


def event_for_image(ev, filename):
    """Камера конкретного фото по имени файла (D01-1.jpg -> D1)."""
    m = RE_IMG_CH.match(filename)
    if not m:
        return ev
    ch = "D%d" % int(m.group(1))
    if ch == ev["channel"] and ch not in ev["cameras"]:
        return ev
    e = dict(ev)
    e["channel"] = ch
    e["camera"] = safe_name(ev["cameras"].get(ch, ch))
    return e


def extract_images(msg):
    """[(filename, bytes)] — все картинки письма (вложения и inline)."""
    images = []
    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        fname = part.get_filename() or ""
        ctype = part.get_content_type()
        is_img = ctype.startswith("image/") or fname.lower().endswith(IMAGE_EXT)
        if not is_img:
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        if not fname:
            ext = {"image/png": ".png", "image/bmp": ".bmp"}.get(ctype, ".jpg")
            fname = f"image{idx}{ext}"
        images.append((safe_name(fname, f"image{idx}.jpg"), data))
    return images


# ------------------------------------------------------------------- ftp ---

class FtpUploader:
    def __init__(self, c):
        self.c = c
        self.ftp = None
        self._made = set()

    def __enter__(self):
        c = self.c
        self.ftp = ftplib.FTP_TLS() if c["ftp_tls"] else ftplib.FTP()
        self.ftp.encoding = "utf-8"
        self.ftp.connect(c["ftp_host"], c["ftp_port"], timeout=30)
        self.ftp.login(c["ftp_user"], c["ftp_password"])
        if c["ftp_tls"]:
            self.ftp.prot_p()
        self.ftp.set_pasv(c["ftp_passive"])
        return self

    def __exit__(self, *exc):
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass

    def _ensure_dir(self, path):
        cur = "/" if path.startswith("/") else ""  # относительный путь — от домашней папки
        for seg in [p for p in path.split("/") if p]:
            cur = cur + seg if cur in ("", "/") else cur + "/" + seg
            if cur in self._made:
                continue
            try:
                self.ftp.mkd(cur)
            except ftplib.error_perm as e:
                if not str(e).startswith("550") and not str(e).startswith("521"):
                    raise  # 550/521 = уже существует
            self._made.add(cur)

    def upload(self, remote_path, data):
        # Имя уникально (время события + номер кадра); при повторной обработке
        # письма файл просто перезапишется тем же фото
        self._ensure_dir(remote_path.rsplit("/", 1)[0])
        self.ftp.storbinary(f"STOR {remote_path}", io.BytesIO(data))
        return remote_path

    def download(self, remote_path):
        buf = io.BytesIO()
        self.ftp.retrbinary(f"RETR {remote_path}", buf.write)
        return buf.getvalue()

    def move(self, src, dst):
        self._ensure_dir(dst.rsplit("/", 1)[0])
        self.ftp.rename(src, dst)

    def delete(self, path):
        self.ftp.delete(path)

    def listdir(self, path):
        """[(имя, это_папка, время_изменения_локальное|None)]"""
        try:
            out = []
            for name, facts in self.ftp.mlsd(path, facts=["type", "modify"]):
                kind = facts.get("type", "")
                if name in (".", "..") or kind in ("cdir", "pdir"):
                    continue
                out.append((name, kind == "dir", ftp_time(facts.get("modify"))))
            return out
        except ftplib.error_perm:
            # сервер без MLSD: папки узнаём по имени-дате, время файлов — через MDTM
            out = []
            for full in self.ftp.nlst(path):
                name = full.rstrip("/").rsplit("/", 1)[-1]
                if name in (".", ".."):
                    continue
                if RE_DATE_DIR.search(name):
                    out.append((name, True, None))
                elif name.lower().endswith(IMAGE_EXT):
                    mdtm = self.ftp.voidcmd(f"MDTM {path.rstrip('/')}/{name}")
                    out.append((name, False, ftp_time(mdtm.split()[-1])))
            return out

    def append(self, remote_path, data, header=b""):
        """Дописать в конец файла (журнал); новый файл начинается с header."""
        self._ensure_dir(remote_path.rsplit("/", 1)[0])
        try:
            self.ftp.size(remote_path)
        except ftplib.error_perm:
            data = header + data
        self.ftp.storbinary(f"APPE {remote_path}", io.BytesIO(data))


class FtpPool:
    """Несколько FTP-соединений для параллельной загрузки: основное время
    уходит на задержку сети на каждый файл, а не на передачу данных."""

    def __init__(self, c, size):
        self._free = queue.Queue()
        self._all = []
        for _ in range(max(1, size)):
            try:
                conn = FtpUploader(c).__enter__()
            except ftplib.all_errors as e:
                if not self._all:
                    raise
                log.warning("FTP: открыто соединений %d из %d (%s)", len(self._all), size, e)
                break
            self._all.append(conn)
            self._free.put(conn)
        self.size = len(self._all)

    def call(self, method, *args):
        """Выполнить метод FtpUploader на свободном соединении."""
        conn = self._free.get()
        try:
            return getattr(conn, method)(*args)
        finally:
            self._free.put(conn)

    def upload(self, remote_path, data):
        return self.call("upload", remote_path, data)

    def append(self, remote_path, data, header=b""):
        return self.call("append", remote_path, data, header)

    def download(self, remote_path):
        return self.call("download", remote_path)

    def close(self):
        for conn in self._all:
            conn.__exit__(None, None, None)


def build_remote_path(c, ev, filename):
    rel = c["path_template"].format(
        camera=ev["camera"], channel=ev["channel"], event=ev["event"], device=ev["device"],
        date=ev["time"].strftime("%Y-%m-%d"), time=ev["time"].strftime("%H%M%S"),
        year=ev["time"].strftime("%Y"), month=ev["time"].strftime("%m"),
        day=ev["time"].strftime("%d"), filename=filename,
    )
    root = "/" + c["ftp_root"].strip("/")
    return (root.rstrip("/") + "/" + rel.lstrip("/")).replace("//", "/")


# --------------------------------------------------------------- analyze ---

def make_detector(c):
    if not c["analyze"]:
        return None
    try:
        import analyze
        detector = analyze.Detector(c["yolo_model"], c["confidence"])
    except ImportError as e:
        log.warning("Анализ фото выключен: не установлены зависимости (%s)", e)
        return None
    log.info("Детектор: %s", c["yolo_model"])
    return detector


def analyze_batch(c, detector, pool, frames, files):
    """Детектор по фото пачки; кадры с объектами — в журнал на FTP.
    Возвращает новые кадры (в порядке files) или None, если анализ не удался."""
    import analyze
    if not files:
        return []
    try:
        counts = detector.counts([blob for _, blob, _ in files])
    except Exception as e:  # анализ не должен ломать загрузку фото
        log.warning("Анализ пачки не удался: %s", e)
        return None
    # n is None — снимок не читается (оборванная загрузка): в отчёт не идёт
    new = [{"time": evi["time"], "camera": evi["camera"], "file": remote,
            "blob": blob if n else None, "counts": n or {}, "bad": n is None}
           for (remote, blob, evi), n in zip(files, counts)]
    frames.extend(new)
    log.info("Анализ: кадров %d, с объектами %d, нечитаемых %d", len(new),
             sum(1 for f in new if f["counts"]), sum(1 for f in new if f["bad"]))
    if not pool:
        return new
    by_day = defaultdict(list)
    for f in new:
        if f["counts"]:
            by_day[f["time"].strftime("%Y-%m-%d")] += analyze.journal_lines([f])
    for day, lines in by_day.items():
        path = f"{c['journal_dir'].rstrip('/')}/{day}.csv"
        try:
            pool.append(path, "".join(lines).encode("utf-8"),
                        analyze.JOURNAL_HEADER.encode("utf-8"))
        except ftplib.all_errors as e:
            log.warning("Журнал %s не записан: %s", path, e)
    return new


def send_report(c, frames, dry_run):
    import analyze
    try:
        report = analyze.build_report(c, frames)
        if not report:
            return
        subject, body, keys = report
        log.info("Отчёт: %s", subject)  # текст отчёта — только в письме: логи Actions публичны
        if dry_run or not c["report_to"]:
            log.info("Отчёт не отправлен (%s)", "dry-run" if dry_run else "не задан получатель")
            return
        analyze.send_mail(c, subject, body, keys)
        log.info("Отчёт отправлен, приложено кадров: %d", len(keys))
    except Exception as e:  # фото уже на FTP — ошибка отчёта не должна ронять прогон
        log.error("Отчёт не отправлен: %s", e)


def walk_images(pool, path, mark=None, depth=3):
    """[(время, путь)] фото в папке и подпапках (до depth уровней), новее mark.
    Папки-даты старше mark пропускаются целиком."""
    out = []
    for name, is_dir, t in pool.call("listdir", path):
        full = f"{path.rstrip('/')}/{name}"
        if is_dir:
            m = RE_DATE_DIR.search(name)
            if depth <= 0:
                continue
            if m and mark:
                y, mo, dd = m.group(4, 5, 6) if m.group(4) else m.group(1, 2, 3)
                if datetime(int(y), int(mo), int(dd)).date() < mark.date():
                    continue
            out += walk_images(pool, full, mark, depth - 1)
        elif name.lower().endswith(IMAGE_EXT) and t and (mark is None or t > mark):
            out.append((t, full))
    return out


def quarantine_path(c, d, path):
    """IPCAM_HOME4/2026_10_11/x.jpg -> IPCAM_HOME4_empty/2026_10_11/x.jpg"""
    root = d.rstrip("/")
    return root + c["quarantine_suffix"] + path[len(root):]


def quarantine(c, pool, ex, empty, dry_run):
    """Снимки без людей/машин/собак/кошек — в карантинную папку рядом."""
    if not empty:
        return
    if dry_run:
        log.info("[dry-run] в карантин ушло бы снимков: %d", len(empty))
        return

    def move(item):
        path, d = item
        try:
            pool.call("move", path, quarantine_path(c, d, path))
            return True
        except ftplib.all_errors as e:
            log.warning("Не перенесён в карантин %s: %s", path, e)
            return False

    log.info("В карантин перенесено пустых снимков: %d из %d", sum(ex.map(move, empty)), len(empty))


def purge_quarantine(c, pool, dry_run):
    """Окончательно удаляет из карантина снимки старше quarantine_days."""
    cutoff = datetime.now() - timedelta(days=c["quarantine_days"])
    for d in c["cleanup_dirs"]:
        q = d.rstrip("/") + c["quarantine_suffix"]
        try:
            old = [p for t, p in walk_images(pool, q) if t < cutoff]
        except ftplib.error_perm:
            continue  # карантина ещё нет
        if not old:
            continue
        if dry_run:
            log.info("[dry-run] из %s удалилось бы старых снимков: %d", q, len(old))
            continue
        deleted = 0
        for p in old:
            try:
                pool.call("delete", p)
                deleted += 1
            except ftplib.all_errors as e:
                log.warning("Не удалён %s: %s", p, e)
        log.info("Карантин %s: удалено снимков старше %g дн.: %d", q, c["quarantine_days"], deleted)


def process_ipcam(c, dry_run, detector, frames):
    """Новые фото камер в папках IPCAM_HOME* на FTP: анализируем; в папках из
    cleanup_dirs пустые снимки уходят в карантин. Что уже обработано — в ipcam_state.json."""
    state_path = f"{c['journal_dir'].rstrip('/')}/ipcam_state.json"
    pool = FtpPool(c, c["ftp_connections"])
    try:
        try:
            state = json.loads(pool.download(state_path).decode("utf-8"))
        except ftplib.error_perm:
            state = {}  # первый запуск
        purge_quarantine(c, pool, dry_run)
        first_mark = datetime.now() - timedelta(hours=c["ipcam_lookback"])
        todo = []  # (время, путь, камера, папка)
        for d in c["ipcam_dirs"]:
            camera = safe_name(d.rstrip("/").rsplit("/", 1)[-1])
            mark = datetime.strptime(state[d], "%Y-%m-%d %H:%M:%S") if d in state else first_mark
            if c["ipcam_since"] and d in c["cleanup_dirs"]:
                since = datetime.strptime(c["ipcam_since"][:10], "%Y-%m-%d")
                if since < mark:
                    log.info("IPCAM %s: разовый проход с %s", camera, f"{since:%d.%m.%Y}")
                    mark = since
            # снимки моложе 3 минут камера может ещё дописывать — возьмём в следующий раз
            settled = datetime.now() - timedelta(minutes=3)
            try:
                found = [(t, path, camera, d) for t, path in walk_images(pool, d, mark)
                         if t <= settled]
            except ftplib.error_perm as e:
                log.warning("IPCAM %s: папка недоступна (%s)", camera, e)
                continue
            log.info("IPCAM %s: новых фото %d (после %s)", camera, len(found), f"{mark:%d.%m %H:%M}")
            if not found:
                # ничего нового — сдвигаем отметку, чтобы не листать старое заново
                state[d] = max(mark, settled).strftime("%Y-%m-%d %H:%M:%S")
            todo += found
        todo.sort()
        if len(todo) > c["ipcam_max"]:
            log.warning("IPCAM: новых фото %d, берём первые %d — остальные в следующий раз",
                        len(todo), c["ipcam_max"])
            todo = todo[:c["ipcam_max"]]

        with ThreadPoolExecutor(max_workers=pool.size) as ex:
            for i in range(0, len(todo), 48):
                chunk = todo[i:i + 48]
                blobs = list(ex.map(lambda x: pool.download(x[1]), chunk))
                new = analyze_batch(c, detector, None if dry_run else pool, frames,
                                    [(path, blob, {"time": t, "camera": cam})
                                     for (t, path, cam, _), blob in zip(chunk, blobs)])
                if new is not None:  # анализ не удался — ничего не трогаем
                    # пустые и нечитаемые: моложе 3 минут сюда не попадают, значит
                    # нечитаемый снимок испорчен окончательно
                    empty = [(f["file"], d) for f, (_, _, _, d) in zip(new, chunk)
                             if not f["counts"] and d in c["cleanup_dirs"]]
                    quarantine(c, pool, ex, empty, dry_run)
                for t, _, _, d in chunk:  # todo отсортирован — последний и есть отметка
                    state[d] = t.strftime("%Y-%m-%d %H:%M:%S")
    finally:
        try:
            if not dry_run and "state" in locals():
                pool.upload(state_path, json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))
        finally:
            pool.close()


# ------------------------------------------------------------------ imap ---

def imap_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_once(c, dry_run=False):
    detector = make_detector(c)
    frames = []  # кадры для отчёта: {time, camera, file, blob, counts}
    process_mail(c, dry_run, detector, frames)
    if c["ipcam_dirs"]:
        if detector:
            try:
                process_ipcam(c, dry_run, detector, frames)
            except ftplib.all_errors as e:  # почта уже обработана — отчёт всё равно шлём
                log.error("IPCAM: ошибка FTP: %s", e)
        else:
            log.info("IPCAM: анализ выключен — папки не проверяем")
    if frames and c["report"]:
        send_report(c, frames, dry_run)


def process_mail(c, dry_run, detector, frames):
    imap = imaplib.IMAP4_SSL(c["imap_host"], c["imap_port"])
    try:
        imap.login(c["imap_user"], c["imap_password"])
        typ, _ = imap.select(imap_quote(c["imap_folder"]))
        if typ != "OK":
            raise RuntimeError(f"Не удалось открыть папку {c['imap_folder']}")

        criteria = ["UNSEEN"]
        if c["imap_from"]:
            criteria += ["FROM", imap_quote(c["imap_from"])]
        if c["imap_subject"]:
            criteria += ["SUBJECT", imap_quote(c["imap_subject"])]
        typ, data = imap.uid("SEARCH", None, *criteria)
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        if not uids:
            log.info("Новых писем нет")
            return 0
        uids = uids[: c["max_per_run"]]
        log.info("Найдено писем: %d", len(uids))
        is_gmail = "gmail" in c["imap_host"]
        deleted, need_expunge = 0, False

        uploaded_total = 0
        pool = None
        if not dry_run:
            pool = FtpPool(c, c["ftp_connections"])
            log.info("FTP-соединений: %d", pool.size)
        try:
            with ThreadPoolExecutor(max_workers=pool.size if pool else 1) as ex:
                for start in range(0, len(uids), c["batch_size"]):
                    # 1. Забираем пачку писем.
                    # BODY.PEEK — не помечает письмо прочитанным до успешной загрузки
                    batch = []  # [(uid, [(remote, blob, event), ...])]
                    for uid in uids[start:start + c["batch_size"]]:
                        typ, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                        raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
                        if typ != "OK" or raw is None:
                            log.warning("UID %s: не удалось получить письмо", uid.decode())
                            continue
                        msg = email.message_from_bytes(raw, policy=email.policy.default)
                        ev = parse_event(msg)
                        images = extract_images(msg)
                        if not images:
                            log.info("UID %s «%s»: картинок нет, пропуск",
                                     uid.decode(), str(msg.get("Subject", "")))
                        files = []
                        for fname, blob in images:
                            evi = event_for_image(ev, fname)
                            files.append((build_remote_path(c, evi, fname), blob, evi))
                        batch.append((uid, files))

                    # 2. Все фото пачки — параллельно по нескольким соединениям
                    failed, first_err = set(), None
                    if dry_run:
                        for uid, files in batch:
                            for remote, blob, _ in files:
                                log.info("[dry-run] %s (%d байт)", remote, len(blob))
                                uploaded_total += 1
                    else:
                        jobs = [(ex.submit(pool.upload, remote, blob), uid, remote, len(blob))
                                for uid, files in batch for remote, blob, _ in files]
                        for fut, uid, remote, size in jobs:
                            try:
                                fut.result()
                                log.info("Загружено: %s (%d байт)", remote, size)
                                uploaded_total += 1
                            except ftplib.all_errors as e:
                                log.error("UID %s: ошибка FTP %s: %s — письмо оставлено непрочитанным",
                                          uid.decode(), remote, e)
                                failed.add(uid)
                                first_err = first_err or e

                        # 3. Помечаем/удаляем только письма, все фото которых уже на FTP;
                        # письма без картинок (потеря видео, ошибка диска...) не удаляются.
                        # Одна команда на всю пачку вместо трёх на каждое письмо.
                        done = [uid for uid, files in batch if uid not in failed]
                        trash = [uid for uid, files in batch if uid not in failed and files]
                        if done:
                            uidset = b",".join(done).decode()
                            imap.uid("STORE", uidset, "+FLAGS", "(\\Seen)")
                            if c["done_label"] and is_gmail:
                                imap.uid("STORE", uidset, "+X-GM-LABELS", imap_quote(c["done_label"]))
                        if c["delete_after"] and trash:
                            uidset = b",".join(trash).decode()
                            if is_gmail:
                                # В Gmail — в Корзину (очищается сама через 30 дней)
                                imap.uid("STORE", uidset, "+X-GM-LABELS", "(\\Trash)")
                            else:
                                imap.uid("STORE", uidset, "+FLAGS", "(\\Deleted)")
                                need_expunge = True
                            deleted += len(trash)

                    # 4. Анализ кадров пачки (люди/машины/собаки/кошки) + журнал на FTP
                    if detector:
                        analyze_batch(c, detector, pool, frames,
                                      [f for uid, files in batch if uid not in failed for f in files])
                    if first_err:
                        raise first_err
        finally:
            if pool:
                pool.close()
            if need_expunge:
                imap.expunge()
            if deleted:
                log.info("Удалено писем: %d", deleted)
        log.info("Готово, загружено файлов: %d", uploaded_total)
        return uploaded_total
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ------------------------------------------------------------------ main ---

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Hikvision e-mail snapshots -> FTP")
    ap.add_argument("-c", "--config", default=os.path.join(here, "config.ini"))
    ap.add_argument("--loop", action="store_true", help="работать постоянно")
    ap.add_argument("--dry-run", action="store_true", help="без загрузки и пометок")
    args = ap.parse_args()

    c = load_config(args.config)
    handlers = [logging.StreamHandler()]
    if c["log_file"]:
        handlers.append(logging.FileHandler(c["log_file"], encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not args.loop:
        try:
            run_once(c, args.dry_run)
        except Exception as e:
            log.error("Ошибка: %s", e)
            sys.exit(1)
        return

    while True:
        try:
            run_once(c, args.dry_run)
        except Exception as e:
            log.error("Ошибка прохода: %s", e)
        time.sleep(c["interval"])


if __name__ == "__main__":
    main()
