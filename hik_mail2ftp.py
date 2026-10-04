#!/usr/bin/env python3
"""
Hikvision NVR/DVR -> Gmail -> FTP.

Забирает непрочитанные письма от регистратора Hikvision из ящика Gmail (IMAP),
вытаскивает фото из вложений и заливает их на FTP.
После успешной загрузки письмо помечается прочитанным и получает метку Gmail
(по умолчанию "hik-processed"), а при delete_after_upload = true — уходит
в Корзину Gmail. Если загрузка не удалась — письмо остаётся
непрочитанным и будет обработано при следующем запуске.

Только стандартная библиотека Python 3.8+.

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
import logging
import os
import queue
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
        cur = ""
        for seg in [p for p in path.split("/") if p]:
            cur += "/" + seg
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

    def upload(self, remote_path, data):
        conn = self._free.get()
        try:
            return conn.upload(remote_path, data)
        finally:
            self._free.put(conn)

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


# ------------------------------------------------------------------ imap ---

def imap_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_once(c, dry_run=False):
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
                    batch = []  # [(uid, [(remote, blob), ...])]
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
                        files = [(build_remote_path(c, event_for_image(ev, fname), fname), blob)
                                 for fname, blob in images]
                        batch.append((uid, files))

                    # 2. Все фото пачки — параллельно по нескольким соединениям
                    failed, first_err = set(), None
                    if dry_run:
                        for uid, files in batch:
                            for remote, blob in files:
                                log.info("[dry-run] %s (%d байт)", remote, len(blob))
                                uploaded_total += 1
                        continue
                    jobs = [(ex.submit(pool.upload, remote, blob), uid, remote, len(blob))
                            for uid, files in batch for remote, blob in files]
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
                    # письма без картинок (потеря видео, ошибка диска...) не удаляются
                    for uid, files in batch:
                        if uid in failed:
                            continue
                        imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                        if c["done_label"] and is_gmail:
                            imap.uid("STORE", uid, "+X-GM-LABELS", imap_quote(c["done_label"]))
                        if c["delete_after"] and files:
                            if is_gmail:
                                # В Gmail — в Корзину (очищается сама через 30 дней)
                                imap.uid("STORE", uid, "+X-GM-LABELS", "(\\Trash)")
                            else:
                                imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                                need_expunge = True
                            deleted += 1
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
