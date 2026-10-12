"""
Анализ фото с камер: люди, машины, собаки, кошки (детектор YOLO),
«визиты» (появление -> исчезновение) и отчёт на почту с текстом от Claude.

Зависимости (ставятся в GitHub Actions): ultralytics, anthropic.
Без них основной скрипт работает как раньше, просто без анализа.
"""

import base64
import io
import logging
import smtplib
from collections import defaultdict
from datetime import timedelta
from email.message import EmailMessage

log = logging.getLogger("hik_mail2ftp")

# Классы COCO -> что считаем
KINDS = {
    "person": "люди",
    "car": "машины", "truck": "машины", "bus": "машины", "motorcycle": "машины",
    "dog": "собаки",
    "cat": "кошки",
}
ORDER = ["люди", "собаки", "кошки", "машины"]  # приоритет для ключевых кадров
JOURNAL_HEADER = "time,camera,file," + ",".join(ORDER) + "\n"


class Detector:
    def __init__(self, model_name, confidence):
        from ultralytics import YOLO  # тяжёлый импорт — только когда анализ включён
        self.model = YOLO(model_name)
        self.confidence = confidence

    def counts(self, blobs, chunk=16):
        """[{kind: count} | None] для каждого JPEG из blobs; None — файл не читается
        (битый или ещё не докачан камерой)."""
        from PIL import Image
        out = [None] * len(blobs)
        for i in range(0, len(blobs), chunk):
            imgs, idx = [], []
            for j, b in enumerate(blobs[i:i + chunk], start=i):
                try:
                    img = Image.open(io.BytesIO(b))
                    img.load()
                    imgs.append(img.convert("RGB"))
                    idx.append(j)
                except Exception as e:
                    log.warning("Снимок %d не читается: %s", j, e)
            if not imgs:
                continue
            for j, r in zip(idx, self.model.predict(imgs, conf=self.confidence, verbose=False)):
                cnt = defaultdict(int)
                for cls in r.boxes.cls.tolist():
                    kind = KINDS.get(r.names[int(cls)])
                    if kind:
                        cnt[kind] += 1
                out[j] = dict(cnt)
        return out


def journal_lines(frames):
    """CSV-строки журнала (только кадры, где что-то найдено)."""
    lines = []
    for f in frames:
        if f["counts"]:
            lines.append(",".join([f["time"].strftime("%Y-%m-%d %H:%M:%S"), f["camera"], f["file"]]
                                  + [str(f["counts"].get(k, 0)) for k in ORDER]) + "\n")
    return lines


def visits(frames, gap_minutes):
    """Склеивает кадры в визиты: объект виден, потом пропал больше чем на gap_minutes."""
    by_key = defaultdict(list)
    for f in frames:
        for kind, n in f["counts"].items():
            by_key[(f["camera"], kind)].append((f["time"], n, f))
    gap = timedelta(minutes=gap_minutes)
    result = []
    for (camera, kind), items in by_key.items():
        items.sort(key=lambda x: x[0])
        cur = None
        for t, n, f in items:
            if cur and t - cur["end"] <= gap:
                cur["end"] = t
                cur["max"] = max(cur["max"], n)
                cur["frames"] += 1
            else:
                cur = {"camera": camera, "kind": kind, "start": t, "end": t,
                       "max": n, "frames": 1, "first": f}
                result.append(cur)
    result.sort(key=lambda v: (v["start"], v["camera"]))
    return result


def visits_table(vs):
    rows = []
    for v in vs:
        span = v["start"].strftime("%d.%m %H:%M")
        if v["end"] != v["start"]:
            span += "–" + v["end"].strftime("%H:%M")
        rows.append(f"{span}  {v['camera']}  {v['kind']}: до {v['max']} одновременно, кадров {v['frames']}")
    return "\n".join(rows)


def key_frames(vs, limit):
    """Первые кадры визитов: сначала люди и животные, потом машины."""
    ranked = sorted(vs, key=lambda v: (ORDER.index(v["kind"]), v["start"]))
    seen, out = set(), []
    for v in ranked:
        f = v["first"]
        if f["file"] not in seen:
            seen.add(f["file"])
            out.append((v, f))
        if len(out) >= limit:
            break
    return sorted(out, key=lambda x: x[1]["time"])


PROMPT = """Ты помощник охраны частного участка. Камеры регистратора Hikvision снимают
только при движении (по 3 кадра на событие), детектор YOLO посчитал объекты на кадрах.

Период: {period}
Камеры с событиями: {cameras}
Всего кадров: {frames}

Визиты (объект впервые замечен -> последний кадр перед паузой больше {gap} мин):
{table}

Выше приложены ключевые кадры — первые кадры некоторых визитов.
{pets}

Напиши короткий отчёт на русском (до 15 строк, простой текст без таблиц и markdown):
- главное: когда и где появлялись люди, машины, собаки, кошки, сколько раз;
- необычное: движение ночью (22:00–06:00), долгое присутствие, много людей сразу;
- если по кадру видно, что детектор ошибся (например, «человек» — это тень) — скажи.
«Исчез» значит лишь «последний кадр с объектом» — камеры снимают только при движении."""


def claude_text(c, period, vs, frames, keys):
    import anthropic
    client = anthropic.Anthropic()  # ключ из ANTHROPIC_API_KEY
    content = []
    for v, f in keys:
        content.append({"type": "text",
                        "text": f"Кадр {f['time']:%H:%M:%S}, {f['camera']}, детектор: {v['kind']}"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(f["blob"]).decode()}})
    pets = ""
    if c.get("pets"):
        pets = ("\nНа участке живут свои собаки — это не посторонние. Называй их по именам, "
                "если узнаёшь на кадре; если по кадру не различить (например, две рыжие) — "
                "так и скажи:\n" + c["pets"] + "\n")
    content.append({"type": "text", "text": PROMPT.format(
        period=period, cameras=", ".join(sorted({f["camera"] for f in frames})),
        frames=len(frames), gap=c["visit_gap"], table=visits_table(vs), pets=pets)})
    resp = client.beta.messages.create(
        model=c["claude_model"],
        max_tokens=8000,
        output_config={"effort": "low"},
        # при отказе фильтра безопасности запрос сам повторится на другой модели
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        log.warning("Claude отказался писать отчёт: %s", resp.stop_details)
        return None
    return "\n".join(b.text for b in resp.content if b.type == "text").strip() or None


def build_report(c, frames):
    """(тема, текст, ключевые кадры) или None, если ничего не найдено."""
    vs = visits(frames, c["visit_gap"])
    if not vs and not c["send_empty"]:
        log.info("Отчёт: объектов не найдено, письмо не отправляем")
        return None
    t0 = min(f["time"] for f in frames)
    t1 = max(f["time"] for f in frames)
    period = f"{t0:%d.%m %H:%M} – {t1:%d.%m %H:%M}"
    totals = defaultdict(int)
    for v in vs:
        totals[v["kind"]] += 1
    summary = ", ".join(f"{k} {totals[k]}" for k in ORDER if totals[k]) or "никого"
    keys = key_frames(vs, c["max_images"])

    text = None
    if vs:
        try:
            text = claude_text(c, period, vs, frames, keys)
        except Exception as e:  # отчёт без Claude лучше, чем никакого
            log.warning("Claude недоступен (%s) — отчёт только по данным детектора", e)
    body = (text or "Объекты по данным детектора (текстовое описание недоступно).")
    if vs:
        body += "\n\n— Детали (визиты) —\n" + visits_table(vs)
    body += f"\n\nКадров обработано: {len(frames)}"
    return f"Камеры {period}: {summary}", body, keys


def send_mail(c, subject, body, keys):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c["imap_user"]
    msg["To"] = c["report_to"]
    msg.set_content(body)
    for v, f in keys:
        msg.add_attachment(f["blob"], maintype="image", subtype="jpeg",
                           filename=f"{f['time']:%H%M%S}_{f['camera']}_{v['kind']}.jpg")
    with smtplib.SMTP_SSL(c["smtp_host"], c["smtp_port"], timeout=60) as s:
        s.login(c["imap_user"], c["imap_password"])
        s.send_message(msg)
