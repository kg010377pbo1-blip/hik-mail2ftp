#!/usr/bin/env python3
"""
Hikvision NVR/DVR -> Gmail -> FTP.

Забирает непрочитанные письма от регистратора Hikvision из ящика Gmail (IMAP),
вытаскивает фото из вложений и заливает их на FTP.
После успешной загрузки письмо помечается прочитанным и получает метку Gmail
(по умолчанию "hik-processed"). Если загрузка не удалась — письмо остаётся
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
import re
import sys
import time
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
        "ftp_host": get("ftp", "host", required=True),
        "ftp_port": int(get("ftp", "port", "21")),
        "ftp_user": get("ftp", "user", required=True),
        "ftp_password": get("ftp", "password", required=True),
        "ftp_tls": get_bool("ftp", "tls", False),
        "ftp_passive": get_bool("ftp", "passive", True),
        "ftp_root": get("ftp", "root", "/") or "/",
        "path_template": get("ftp", "path_template", "{camera}/{date}/{time}_{filename}"),
        "interval": int(get("main", "interval_seconds", "60")),
        "max_per_run": int(get("main", "max_messages_per_run", "200")),
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

    return {
        "camera": safe_name(camera or first(RE_CAMERA, subject), "camera"),
        "channel": safe_name(channel, "ch"),
        "event": safe_name(first(RE_EVENT_TYPE, text) or subject, "event"),
        "device": safe_name(first(RE_DEVICE, text), "nvr"),
        "time": ev_time,
    }


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

    def _exists(self, path):
        try:
            self.ftp.size(path)
            return True
        except ftplib.all_errors:
            return False

    def upload(self, remote_path, data):
        d, base = remote_path.rsplit("/", 1)
        self._ensure_dir(d)
        # не перезатираем файл с тем же именем
        stem, ext = os.path.splitext(base)
        target, n = remote_path, 1
        while self._exists(target):
            target = f"{d}/{stem}_{n}{ext}"
            n += 1
        self.ftp.storbinary(f"STOR {target}", io.BytesIO(data))
        return target


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

        uploaded_total = 0
        uploader = None if dry_run else FtpUploader(c).__enter__()
        try:
            for uid in uids:
                # BODY.PEEK — не помечает письмо прочитанным до успешной загрузки
                typ, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
                if typ != "OK" or raw is None:
                    log.warning("UID %s: не удалось получить письмо", uid.decode())
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                ev = parse_event(msg)
                images = extract_images(msg)
                subj = str(msg.get("Subject", ""))
                if not images:
                    log.info("UID %s «%s»: картинок нет, пропуск", uid.decode(), subj)
                else:
                    try:
                        for fname, blob in images:
                            remote = build_remote_path(c, ev, fname)
                            if dry_run:
                                log.info("[dry-run] %s (%d байт)", remote, len(blob))
                            else:
                                remote = uploader.upload(remote, blob)
                                log.info("Загружено: %s (%d байт)", remote, len(blob))
                            uploaded_total += 1
                    except ftplib.all_errors as e:
                        log.error("UID %s: ошибка FTP: %s — письмо оставлено непрочитанным",
                                  uid.decode(), e)
                        raise
                if not dry_run:
                    imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                    if c["done_label"] and "gmail" in c["imap_host"]:
                        imap.uid("STORE", uid, "+X-GM-LABELS", imap_quote(c["done_label"]))
        finally:
            if uploader:
                uploader.__exit__(None, None, None)
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
