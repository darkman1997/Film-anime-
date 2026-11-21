#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shoponline.py — Bot Telegram che monitora un canale Telegram pubblico
(default @shoponline922) tramite anteprima web t.me/s/<canale> e avvisa
quando escono post compatibili con le richieste salvate.

Solo libreria standard di Python 3.8+ (nessun pip install).

Variabili d'ambiente:
  TELEGRAM_TOKEN        obbligatoria  token del bot (da @BotFather)
  TELEGRAM_CHAT_ID      facoltativa   default: 8798296477
  CANALE                facoltativa   default: shoponline922
  STATO                 facoltativa   default: shoponline-stato.json
  SECONDI_ASCOLTO       facoltativa   default: 420
  INTERVALLO_CONTROLLO  facoltativa   default: 60
  ATTESA_POLL           facoltativa   default: 20
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


def _num(nome: str, default: int) -> int:
    try:
        return int(os.environ.get(nome) or default)
    except ValueError:
        return default


TG_BASE = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "8798296477").strip()
CANALE = (os.environ.get("CANALE") or "shoponline922").strip().lstrip("@")
STATO = (os.environ.get("STATO") or "shoponline-stato.json").strip()

SECONDI_ASCOLTO = _num("SECONDI_ASCOLTO", 420)
INTERVALLO_CONTROLLO = _num("INTERVALLO_CONTROLLO", 60)
ATTESA_POLL = _num("ATTESA_POLL", 20)
MAX_INIZIALI = 8
MAX_POST_GIRO = 15

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Rete & Telegram
# --------------------------------------------------------------------------

def _apri(url: str) -> str:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "it-IT,it;q=0.9,en;q=0.6")
    req.add_header("Accept-Encoding", "gzip")
    with urllib.request.urlopen(req, timeout=30) as r:
        dati = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                dati = gzip.decompress(dati)
            except OSError:
                pass
        return dati.decode("utf-8", "replace")


def telegram(metodo: str, **campi) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN mancante")
    dati = urllib.parse.urlencode({k: v for k, v in campi.items() if v is not None}).encode()
    req = urllib.request.Request(TG_BASE.format(token=TELEGRAM_TOKEN, method=metodo), data=dati)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def invia(testo: str, markup: dict | None = None) -> None:
    campi = dict(chat_id=CHAT_ID, text=testo[:4090], parse_mode="HTML", disable_web_page_preview="true")
    if markup:
        campi["reply_markup"] = json.dumps(markup)
    try:
        telegram("sendMessage", **campi)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        campi.update(text=re.sub(r"<[^>]+>", "", testo)[:4090], parse_mode=None)
        telegram("sendMessage", **campi)


def invia_foto(foto: str, didascalia: str, markup: dict | None = None) -> bool:
    if len(didascalia) > 1020:
        return False
    campi = dict(chat_id=CHAT_ID, photo=foto, caption=didascalia, parse_mode="HTML")
    if markup:
        campi["reply_markup"] = json.dumps(markup)
    try:
        telegram("sendPhoto", **campi)
        return True
    except urllib.error.HTTPError:
        return False


def tastiera() -> dict:
    return {
        "keyboard": [
            [{"text": "➕ Nuova richiesta"}, {"text": "📋 Le mie richieste"}],
            [{"text": "🕒 Ultimi post"}, {"text": "📊 Stato"}],
            [{"text": "❓ Aiuto"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def menu_principale() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "➕ Nuova richiesta", "callback_data": "nuova"},
                {"text": "📋 Le mie richieste", "callback_data": "lista"},
            ],
            [
                {"text": "🕒 Ultimi 5 post", "callback_data": "ultimi"},
                {"text": "❓ Come funziona", "callback_data": "aiuto"},
            ],
        ]
    }


# --------------------------------------------------------------------------
# Stato
# --------------------------------------------------------------------------

def stato_vuoto() -> dict:
    return {
        "richieste": [],
        "ultimo_post_id": 0,
        "last_update_id": 0,
        "attivo": True,
        "mod": None,
        "ultimo_giro": "",
    }


def carica_stato() -> dict:
    if not os.path.exists(STATO):
        return stato_vuoto()
    try:
        with open(STATO, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, json.JSONDecodeError):
        return stato_vuoto()
    base = stato_vuoto()
    base.update({k: s.get(k, base[k]) for k in base})
    for r in base["richieste"]:
        r.setdefault("escludi", [])
        r.setdefault("rimborso_min", 0)
        r.setdefault("solo_acquisto", False)
        r.setdefault("pausa", False)
        r.setdefault("esistenti_cercati", True)
    return base


def salva_stato(s: dict) -> None:
    with open(STATO, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2, sort_keys=True)


def chiave_richiesta(r: dict) -> str:
    return "{}|{}|{}|{}".format(
        r["q"].strip().lower(),
        int(r.get("rimborso_min") or 0),
        1 if r.get("solo_acquisto") else 0,
        ",".join(sorted(x.lower() for x in (r.get("escludi") or []))),
    )


# --------------------------------------------------------------------------
# Scraping canale pubblico t.me/s/<canale>
# --------------------------------------------------------------------------

def estrai_rimborso(testo: str) -> int | None:
    basso = (testo or "").lower()
    if "gratis" in basso and ("recensione" in basso or "rimborso" in basso):
        return 100
    m = re.search(r"rimborso\s*:?\s*(\d{1,3})\s*%", basso)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,3})\s*%", basso)
    if m:
        return int(m.group(1))
    return None


def leggi_post_canale(canale: str = CANALE) -> list[dict]:
    url = "https://t.me/s/{}".format(urllib.parse.quote(canale))
    pagina = _apri(url)
    blocchi = pagina.split('<div class="tgme_widget_message_wrap')
    posts: list[dict] = []
    visti: set[int] = set()
    for b in blocchi[1:]:
        pm = re.search(r'data-post="([^"]+/(\d+))"', b)
        if not pm:
            continue
        pid = int(pm.group(2))
        if pid in visti:
            continue
        visti.add(pid)

        tm = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        testo = ""
        if tm:
            raw = re.sub(r"<br\s*/?>", "\n", tm.group(1))
            testo = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()

        im = re.search(
            r"tgme_widget_message_photo_wrap[^>]+style=\"[^\"]*background-image:url\('([^']+)'\)",
            b,
        )
        img = html.unescape(im.group(1)) if im else ""

        bottoni = []
        for href, label in re.findall(
            r'class="tgme_widget_message_inline_button[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            b,
            re.S,
        ):
            lbl = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
            u = html.unescape(href)
            if u.startswith("http"):
                bottoni.append({"text": lbl[:32] or "Apri", "url": u})

        link = "https://t.me/{}/{}".format(canale, pid)
        if not testo and not img:
            continue

        posts.append({
            "id": pid,
            "testo": testo,
            "img": img,
            "link": link,
            "rimborso": estrai_rimborso(testo),
            "solo_acquisto": "solo acquisto" in testo.lower(),
            "bottoni": bottoni,
        })
    posts.sort(key=lambda p: p["id"])
    return posts


# --------------------------------------------------------------------------
# Parsing & Matching richieste
# --------------------------------------------------------------------------

def normalizza(testo: str) -> str:
    t = (testo or "").lower()
    for a, b in (
        ("à", "a"), ("á", "a"), ("è", "e"), ("é", "e"),
        ("ì", "i"), ("í", "i"), ("ò", "o"), ("ó", "o"),
        ("ù", "u"), ("ú", "u"),
    ):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def parse_richiesta(testo: str) -> dict | None:
    """Esempi:
      'cuffie'
      'ssd 100%'
      'notebook 80% -usato -custodia'
      '#elettronica gratis'
      '* 100%'  (tutti i post con rimborso 100%)
      'tutti'   (tutti i post del canale)
    """
    argomenti = (testo or "").split()
    if not argomenti:
        return None
    rimborso_min = 0
    solo_acquisto = False
    escludi: list[str] = []
    parole: list[str] = []

    for tok in argomenti:
        low = tok.lower()
        if low.startswith("-") and len(low) > 2:
            escludi.append(low[1:])
            continue
        if low in ("gratis", "100%"):
            rimborso_min = max(rimborso_min, 100)
            continue
        if low in ("soloacquisto", "solo_acquisto"):
            solo_acquisto = True
            continue
        m = re.fullmatch(r"(\d{1,3})%\+?", low)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 100:
                rimborso_min = max(rimborso_min, val)
                continue
        parole.append(tok)

    if not parole:
        if rimborso_min or solo_acquisto:
            parole = ["*"]
        else:
            return None

    q = " ".join(parole).strip()
    if q.lower() in ("tutto", "tutti", "all"):
        q = "*"

    return {
        "q": q,
        "rimborso_min": rimborso_min,
        "solo_acquisto": solo_acquisto,
        "escludi": escludi,
        "pausa": False,
        "esistenti_cercati": False,
    }


def descrivi_richiesta(r: dict, i: int | None = None) -> str:
    nome = "Tutti i post" if r["q"] == "*" else r["q"]
    extra = []
    if r.get("rimborso_min"):
        extra.append("rimborso ≥{}%".format(int(r["rimborso_min"])))
    if r.get("solo_acquisto"):
        extra.append("solo acquisto")
    if r.get("escludi"):
        extra.append("senza " + ", ".join(r["escludi"]))
    if r.get("pausa"):
        extra.append("⏸ in pausa")
    coda = (" · " + " · ".join(extra)) if extra else ""
    if i is None:
        return "{}{}".format(nome, coda)
    return "{}. {}{}".format(i, nome, coda)


def aggiungi_richiesta(s: dict, testo: str) -> tuple[dict | None, bool]:
    nuova = parse_richiesta(testo)
    if nuova is None:
        return None, False
    k = chiave_richiesta(nuova)
    for r in s["richieste"]:
        if chiave_richiesta(r) == k:
            return r, False
    s["richieste"].append(nuova)
    s["ultima_aggiunta"] = k
    return nuova, True


def post_compatibile(post: dict, r: dict) -> bool:
    if r.get("pausa"):
        return False
    testo_norm = normalizza(post.get("testo") or "")
    if not testo_norm:
        return False
    if r.get("solo_acquisto") and not post.get("solo_acquisto"):
        return False
    rmin = int(r.get("rimborso_min") or 0)
    if rmin > 0:
        if post.get("rimborso") is None or int(post["rimborso"]) < rmin:
            return False
    for ex in r.get("escludi") or []:
        ex_norm = normalizza(ex)
        if ex_norm and ex_norm in testo_norm:
            return False
    q = r["q"].strip()
    if q == "*":
        return True
    for parola in normalizza(q).split():
        if parola not in testo_norm:
            return False
    return True


# --------------------------------------------------------------------------
# Invio post
# --------------------------------------------------------------------------

def invia_post(post: dict, richieste_trovate: list[dict]) -> None:
    nomi = ", ".join(
        ("Tutti i post" if r["q"] == "*" else r["q"]) for r in richieste_trovate
    )
    corpo = html.escape(post.get("testo") or "(post senza testo)")
    intestazione = "🔎 <b>{}</b>  ·  @{}\n\n".format(html.escape(nomi), html.escape(CANALE))
    msg = intestazione + corpo

    righe_bottoni = []
    if post.get("bottoni"):
        righe_bottoni.append(post["bottoni"][:2])
    righe_bottoni.append([{"text": "🔗 Apri post su Telegram", "url": post["link"]}])
    markup = {"inline_keyboard": righe_bottoni}

    ok = False
    if post.get("img"):
        ok = invia_foto(post["img"], msg, markup)
    if not ok:
        invia(msg, markup)


# --------------------------------------------------------------------------
# Comandi Telegram
# --------------------------------------------------------------------------

BENVENUTO = (
    "Ciao! 👋 Monitoro il canale <b>@{canale}</b> per te.\n\n"
    "Scrivimi cosa cerchi e ti avviso subito appena esce un post compatibile "
    "(e ti mostro subito eventuali post già usciti tra gli ultimi).\n\n"
    "<b>Esempi rapidi:</b>\n"
    "• <code>cuffie</code> — qualsiasi post con la parola cuffie\n"
    "• <code>ssd 100%</code> — solo con rimborso 100% / gratis\n"
    "• <code>#elettronica 80% -custodia</code> — categoria senza una parola\n"
    "• <code>tutti 100%</code> — tutti i post al 100%"
).format(canale=html.escape(CANALE))

AIUTO = (
    "<b>Comandi disponibili</b>\n\n"
    "• <code>/aggiungi parole [100%] [-escludi]</code>\n"
    "  <i>Oppure scrivi direttamente il prodotto in chat senza /</i>\n"
    "• <code>/lista</code> — mostra le richieste (con bottoni ✏️ / ⏸ / 🗑)\n"
    "• <code>/modifica N 100%</code> (o <code>80%</code>, <code>tutte</code>, <code>-parola</code>)\n"
    "• <code>/pausa N</code> / <code>/riprendi N</code> (o <code>tutto</code>)\n"
    "• <code>/togli N</code> — elimina la richiesta numero N\n"
    "• <code>/tutti on</code> / <code>/tutti off</code> — attiva/toglie tutti i post\n"
    "• <code>/ultimi</code> — mostra gli ultimi 5 post del canale\n"
    "• <code>/controlla</code> — controlla subito il canale\n"
    "• <code>/stato</code> — stato del bot e ultimo post letto"
)


def invia_lista(s: dict) -> None:
    if not s["richieste"]:
        invia("Nessuna richiesta attiva al momento. Tocca ➕ o scrivimi cosa cercare.", menu_principale())
        return
    righe = "\n".join("• " + descrivi_richiesta(r, i + 1) for i, r in enumerate(s["richieste"]))
    bottoni = []
    for i, r in enumerate(s["richieste"][:10]):
        nome = "Tutti" if r["q"] == "*" else r["q"][:16]
        icona_pausa = "▶️" if r.get("pausa") else "⏸"
        bottoni.append([
            {"text": f"✏️ {i + 1}. {nome}", "callback_data": f"edit:{i + 1}"},
            {"text": icona_pausa, "callback_data": f"tog:{i + 1}"},
            {"text": "🗑", "callback_data": f"del:{i + 1}"},
        ])
    invia("<b>📋 Le tue richieste</b>\n" + righe, {"inline_keyboard": bottoni})


def menu_modifica(s: dict, i: int) -> None:
    r = s["richieste"][i]
    invia(
        "<b>✏️ Modifico richiesta {}</b>\n{}\n\nScegli il filtro rimborso o usa /modifica {} -parola:".format(
            i + 1, html.escape(descrivi_richiesta(r)), i + 1
        ),
        {
            "inline_keyboard": [
                [
                    {"text": "💯 Solo 100%", "callback_data": f"rim:{i + 1}:100"},
                    {"text": "📈 Almeno 80%", "callback_data": f"rim:{i + 1}:80"},
                    {"text": "🌐 Qualsiasi %", "callback_data": f"rim:{i + 1}:0"},
                ],
                [
                    {"text": "▶️ Riprendi" if r.get("pausa") else "⏸ Metti in pausa", "callback_data": f"tog:{i + 1}"},
                    {"text": "🗑 Elimina", "callback_data": f"del:{i + 1}"},
                ],
                [{"text": "🔙 Lista richieste", "callback_data": "lista"}],
            ]
        },
    )


def cerca_esistenti_richiesta(s: dict, r: dict) -> int:
    try:
        posts = leggi_post_canale()
    except (urllib.error.URLError, OSError) as e:
        print("errore lettura canale: {}".format(e))
        invia("⚠️ Errore temporaneo nel leggere @{}: riprovo al prossimo controllo.".format(html.escape(CANALE)))
        return 0
    if posts:
        s["ultimo_post_id"] = max(int(s.get("ultimo_post_id") or 0), posts[-1]["id"])
    r["esistenti_cercati"] = True
    trovati = [p for p in posts if post_compatibile(p, r)]
    if not trovati:
        invia(
            "Nessun post compatibile tra gli ultimi {} di @{}.\n"
            "Continuo a monitorare e ti avviso appena esce! 👀".format(len(posts), html.escape(CANALE))
        )
        return 0
    inviati = 0
    for p in trovati[-MAX_INIZIALI:]:
        invia_post(p, [r])
        inviati += 1
        time.sleep(0.7)
    if len(trovati) > MAX_INIZIALI:
        invia("…e altri {} post precedenti compatibili (mostrati i {} più recenti).".format(
            len(trovati) - MAX_INIZIALI, inviati
        ))
    invia("✅ Da ora in poi ti avviserò in tempo reale sui nuovi post compatibili!")
    return inviati


def invia_ultimi(quanti: int = 5) -> None:
    try:
        posts = leggi_post_canale()
    except (urllib.error.URLError, OSError) as e:
        invia("⚠️ Impossibile leggere @{}: {}".format(html.escape(CANALE), html.escape(str(e))))
        return
    if not posts:
        invia("Nessun post trovato su @{}.".format(html.escape(CANALE)))
        return
    for p in posts[-quanti:]:
        riga = "<b>Post #{}</b>\n{}".format(p["id"], html.escape((p.get("testo") or "")[:600]))
        invia(riga, {"inline_keyboard": [[{"text": "🔗 Apri post", "url": p["link"]}]]})
        time.sleep(0.5)


def gestisci_comandi(s: dict, attesa: int = 0) -> bool:
    cambiato = False
    try:
        ris = telegram("getUpdates", offset=s.get("last_update_id") or None, timeout=attesa)
    except (urllib.error.URLError, RuntimeError) as e:
        print("getUpdates: {}".format(e))
        return False

    for upd in ris.get("result", []):
        s["last_update_id"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if cq:
            chat_cq = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
            if chat_cq and chat_cq != CHAT_ID:
                continue
            dati = cq.get("data") or ""
            try:
                telegram("answerCallbackQuery", callback_query_id=cq.get("id"))
            except (urllib.error.URLError, RuntimeError):
                pass
            if dati == "lista":
                invia_lista(s)
            elif dati == "aiuto":
                invia(AIUTO, menu_principale())
            elif dati == "ultimi":
                invia_ultimi(5)
            elif dati == "nuova":
                invia("Scrivimi cosa vuoi monitorare (es. <code>cuffie 100%</code> o <code>#elettronica -rotto</code>):")
            elif dati.startswith("edit:") and dati[5:].isdigit():
                i = int(dati[5:]) - 1
                if 0 <= i < len(s["richieste"]):
                    menu_modifica(s, i)
                else:
                    invia("Richiesta non trovata.", menu_principale())
            elif dati.startswith("tog:") and dati[4:].isdigit():
                i = int(dati[4:]) - 1
                if 0 <= i < len(s["richieste"]):
                    r = s["richieste"][i]
                    r["pausa"] = not r.get("pausa")
                    cambiato = True
                    invia(
                        ("⏸ Messa in pausa: " if r["pausa"] else "▶️ Riattivata: ")
                        + descrivi_richiesta(r, i + 1)
                    )
                    invia_lista(s)
            elif dati.startswith("del:") and dati[4:].isdigit():
                i = int(dati[4:]) - 1
                if 0 <= i < len(s["richieste"]):
                    tolta = s["richieste"].pop(i)
                    cambiato = True
                    invia("🗑 Rimossa: " + descrivi_richiesta(tolta))
                    invia_lista(s)
            elif dati.startswith("rim:"):
                parti_cb = dati.split(":")
                if len(parti_cb) == 3 and parti_cb[1].isdigit() and parti_cb[2].isdigit():
                    i = int(parti_cb[1]) - 1
                    if 0 <= i < len(s["richieste"]):
                        s["richieste"][i]["rimborso_min"] = int(parti_cb[2])
                        cambiato = True
                        invia("Aggiornata ✅\n" + descrivi_richiesta(s["richieste"][i], i + 1))
                        invia_lista(s)
            continue

        msg = upd.get("message") or upd.get("edited_message") or {}
        testo = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id") or "")
        if not testo or (chat and chat != CHAT_ID):
            continue

        parti = testo.split()
        cmd = parti[0].split("@")[0].lower()
        argomenti = parti[1:]

        low = testo.lower()
        if "nuova richiesta" in low:
            cmd, argomenti = "/nuova", []
        elif "le mie richieste" in low:
            cmd = "/lista"
        elif "ultimi post" in low:
            cmd = "/ultimi"
        elif low.endswith("stato") and len(parti) <= 2:
            cmd = "/stato"
        elif low.endswith("aiuto") and len(parti) <= 2:
            cmd = "/aiuto"

        if cmd == "/start":
            invia(BENVENUTO, tastiera())
        elif cmd in ("/aiuto", "/help"):
            invia(AIUTO, menu_principale())
        elif cmd == "/nuova":
            invia(
                "🔎 Scrivimi cosa cercare su @{}.\n\n"
                "Esempi:\n"
                "• <code>notebook 100%</code>\n"
                "• <code>#animali -gatti</code>\n"
                "• <code>tutti 100%</code>".format(html.escape(CANALE))
            )
        elif cmd in ("/lista", "/list"):
            invia_lista(s)
        elif cmd in ("/ultimi", "/last"):
            n = int(argomenti[0]) if argomenti and argomenti[0].isdigit() else 5
            invia_ultimi(max(1, min(10, n)))
        elif cmd == "/stato":
            attive = sum(1 for r in s["richieste"] if not r.get("pausa"))
            invia(
                "<b>📊 Stato monitor @{canale}</b>\n"
                "Monitoraggio: {stato}\n"
                "Richieste attive: {attive}/{tot}\n"
                "Ultimo post letto: #{pid}\n"
                "Ultimo controllo: {giro}".format(
                    canale=html.escape(CANALE),
                    stato="attivo ✅" if s.get("attivo", True) else "in pausa ⏸",
                    attive=attive,
                    tot=len(s["richieste"]),
                    pid=s.get("ultimo_post_id") or "—",
                    giro=html.escape(s.get("ultimo_giro") or "—"),
                ),
                menu_principale(),
            )
        elif cmd in ("/aggiungi", "/add"):
            if not argomenti:
                invia("Uso: <code>/aggiungi parole [100%] [-escludi]</code>")
                continue
            nuova, aggiunta = aggiungi_richiesta(s, " ".join(argomenti))
            if nuova is None:
                invia("Non ho capito la richiesta. Esempio: <code>/aggiungi cuffie 100%</code>")
                continue
            if not aggiunta:
                invia("Questa richiesta c'è già: " + descrivi_richiesta(nuova))
                continue
            cambiato = True
            invia("Aggiunta ✅\n" + descrivi_richiesta(nuova, len(s["richieste"])) +
                  "\n🔎 Controllo subito gli ultimi post del canale...")
            cerca_esistenti_richiesta(s, nuova)
        elif cmd in ("/togli", "/rimuovi", "/del"):
            if not argomenti or not argomenti[0].isdigit():
                invia("Specifica il numero: <code>/togli 1</code> (vedi /lista)")
                continue
            i = int(argomenti[0]) - 1
            if 0 <= i < len(s["richieste"]):
                tolta = s["richieste"].pop(i)
                cambiato = True
                invia("🗑 Rimossa: " + descrivi_richiesta(tolta))
            else:
                invia("Numero non valido, vedi /lista.")
        elif cmd == "/annulla":
            ultima = s.pop("ultima_aggiunta", None)
            if ultima:
                for i, r in enumerate(s["richieste"]):
                    if chiave_richiesta(r) == ultima:
                        tolta = s["richieste"].pop(i)
                        cambiato = True
                        invia("Annullata ✅ Rimossa: " + descrivi_richiesta(tolta))
                        break
                else:
                    invia("Niente da annullare 🙂")
            else:
                invia("Niente da annullare 🙂")
        elif cmd in ("/modifica", "/mod"):
            if len(argomenti) < 2 or not argomenti[0].isdigit():
                invia("Uso: <code>/modifica N 100%</code> oppure <code>/modifica N -parola</code>")
                continue
            i = int(argomenti[0]) - 1
            if not (0 <= i < len(s["richieste"])):
                invia("Numero non valido, vedi /lista.")
                continue
            r = s["richieste"][i]
            for tok in argomenti[1:]:
                low_t = tok.lower()
                if low_t in ("tutte", "tutti", "all", "0%"):
                    r["rimborso_min"] = 0
                elif low_t in ("100%", "gratis"):
                    r["rimborso_min"] = 100
                elif re.fullmatch(r"(\d{1,3})%\+?", low_t):
                    r["rimborso_min"] = min(100, int(low_t.rstrip("%+")))
                elif low_t.startswith("-") and len(low_t) > 2:
                    par = low_t[1:]
                    if par not in r["escludi"]:
                        r["escludi"].append(par)
                elif low_t.startswith("+") and len(low_t) > 2:
                    par = low_t[1:]
                    r["escludi"] = [x for x in r["escludi"] if x != par]
            cambiato = True
            invia("Aggiornata ✅\n" + descrivi_richiesta(r, i + 1))
        elif cmd in ("/pausa", "/stop"):
            if argomenti and argomenti[0].isdigit():
                i = int(argomenti[0]) - 1
                if 0 <= i < len(s["richieste"]):
                    s["richieste"][i]["pausa"] = True
                    cambiato = True
                    invia("⏸ In pausa: " + descrivi_richiesta(s["richieste"][i], i + 1))
                else:
                    invia("Numero non valido.")
            else:
                s["attivo"] = False
                cambiato = True
                invia("⏸ Monitoraggio canale messo in pausa. Scrivi /riprendi per riattivarlo.")
        elif cmd in ("/riprendi", "/on"):
            if argomenti and argomenti[0].isdigit():
                i = int(argomenti[0]) - 1
                if 0 <= i < len(s["richieste"]):
                    s["richieste"][i]["pausa"] = False
                    cambiato = True
                    invia("▶️ Riattivata: " + descrivi_richiesta(s["richieste"][i], i + 1))
                else:
                    invia("Numero non valido.")
            else:
                s["attivo"] = True
                s["_forza"] = True
                cambiato = True
                invia("▶️ Monitoraggio canale riattivato ✅")
        elif cmd == "/tutti":
            stato_arg = argomenti[0].lower() if argomenti else "on"
            if stato_arg in ("off", "no", "stop"):
                prima = len(s["richieste"])
                s["richieste"] = [r for r in s["richieste"] if r["q"] != "*"]
                cambiato = len(s["richieste"]) != prima
                invia("Disattivata la ricezione di tutti i post." if cambiato else "Non avevi una regola per tutti i post.")
            else:
                nuova, aggiunta = aggiungi_richiesta(s, "*")
                if aggiunta and nuova:
                    cambiato = True
                    invia("Attivato ✅ Riceverai tutti i nuovi post di @{}.".format(html.escape(CANALE)))
                    cerca_esistenti_richiesta(s, nuova)
                else:
                    invia("Ricevi già tutti i post del canale.")
        elif cmd in ("/controlla", "/check"):
            s["_forza"] = True
            cambiato = True
        elif not testo.startswith("/"):
            nuova, aggiunta = aggiungi_richiesta(s, testo)
            if nuova is None:
                invia("Scrivi almeno una parola da cercare (es. <code>cuffie 100%</code>).")
                continue
            if not aggiunta:
                invia("Questa richiesta c'è già: " + descrivi_richiesta(nuova))
                continue
            cambiato = True
            invia("Aggiunta ✅\n" + descrivi_richiesta(nuova, len(s["richieste"])) +
                  "\n🔎 Controllo subito gli ultimi post... (per annullare: /annulla)")
            cerca_esistenti_richiesta(s, nuova)
        else:
            invia("Comando non riconosciuto.\n\n" + AIUTO)

    return cambiato


# --------------------------------------------------------------------------
# Controllo periodico del canale
# --------------------------------------------------------------------------

def controlla_canale(s: dict, dry: bool = False) -> int:
    inviati = 0
    if not s.get("attivo", True):
        return 0

    for r in list(s["richieste"]):
        if not r.get("esistenti_cercati", True) and not r.get("pausa"):
            inviati += cerca_esistenti_richiesta(s, r)

    try:
        posts = leggi_post_canale()
    except (urllib.error.URLError, OSError) as e:
        print("errore lettura @{}: {}".format(CANALE, e))
        return inviati

    if not posts:
        return inviati

    ultimo_letto = int(s.get("ultimo_post_id") or 0)
    if ultimo_letto == 0:
        # prima partenza assoluta: parte dall'ultimo post online senza raffica
        s["ultimo_post_id"] = posts[-1]["id"]
        s["ultimo_giro"] = datetime.now().isoformat(timespec="minutes")
        if not dry:
            salva_stato(s)
        print("base iniziale @{}: post #{}".format(CANALE, s["ultimo_post_id"]))
        return inviati

    nuovi = [p for p in posts if p["id"] > ultimo_letto]
    attive = [r for r in s["richieste"] if not r.get("pausa")]

    if nuovi and attive:
        da_inviare = []
        for p in nuovi:
            match = [r for r in attive if post_compatibile(p, r)]
            if match:
                da_inviare.append((p, match))
        for p, match in da_inviare[-MAX_POST_GIRO:]:
            if dry:
                print("[DRY] post #{}: {}".format(p["id"], (p.get("testo") or "")[:100]))
            else:
                invia_post(p, match)
                time.sleep(0.7)
            inviati += 1

    s["ultimo_post_id"] = max(ultimo_letto, posts[-1]["id"])
    s["ultimo_giro"] = datetime.now().isoformat(timespec="minutes")
    s.pop("_forza", None)
    if not dry:
        salva_stato(s)
    print("giro @{}: {} nuovi letti, {} inviati".format(CANALE, len(nuovi), inviati))
    return inviati


def ciclo(secondi: int | None = None) -> int:
    durata = max(60, int(secondi or SECONDI_ASCOLTO))
    s = carica_stato()
    fine = time.time() + durata
    prossimo = time.time()
    print("monitor @{} per {}s (controllo ogni {}s)".format(CANALE, durata, INTERVALLO_CONTROLLO))
    while time.time() < fine:
        if time.time() >= prossimo:
            try:
                controlla_canale(s, dry=False)
            except Exception as e:
                print("errore nel controllo: {}".format(e))
            salva_stato(s)
            prossimo = time.time() + INTERVALLO_CONTROLLO
            continue
        try:
            cambiato = gestisci_comandi(s, attesa=ATTESA_POLL)
        except Exception as e:
            print("errore in ascolto: {}".format(e))
            time.sleep(3)
            continue
        if cambiato:
            salva_stato(s)
            if s.get("_forza"):
                prossimo = 0
    salva_stato(s)
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Monitor Telegram per canale pubblico")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--loop", action="store_true", help="ascolta i comandi e controlla il canale")
    g.add_argument("--once", action="store_true", help="un giro singolo (default)")
    g.add_argument("--dry-run", action="store_true", help="legge il canale senza inviare nulla")
    g.add_argument("--test", action="store_true", help="invia un messaggio di prova")
    a = p.parse_args(argv)

    if a.test:
        invia("Monitor @{} attivo ✅".format(html.escape(CANALE)))
        return 0
    if a.loop:
        return ciclo()
    s = carica_stato()
    if not a.dry_run and gestisci_comandi(s):
        salva_stato(s)
    controlla_canale(s, dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
