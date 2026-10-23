#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Bot Telegram "Cinema animazione".

Avvisa quando esce un nuovo film d'animazione:
  * al cinema in Italia (film appena usciti, senza anteprime);
  * sulle piattaforme streaming in Italia (film arrivati da poco).

Dati: TMDB (The Movie Database), API gratuita.
Dipendenze: solo libreria standard di Python 3.8+ (nessun pip install).

Variabili d'ambiente
  TELEGRAM_TOKEN    obbligatoria  token del bot (da @BotFather)
  TMDB_API_KEY      obbligatoria  chiave gratuita TMDB (v3 oppure token v4)
  TELEGRAM_CHAT_ID  facoltativa   a chi mandare il digest (default: la tua chat)
  REGIONE           facoltativa   default IT
  LINGUA            facoltativa   default it-IT
  GIORNI_CINEMA     facoltativa   default 3   (quanti giorni indietro guardare:
                                  avvisa solo film gia' usciti, niente anteprime)
  GIORNI_CATALOGO   facoltativa   default 270 (quanto indietro guardare per lo streaming)
  STATE_FILE        facoltativa   default stato.json (titoli gia' inviati)
  INVIA_SE_VUOTO    facoltativa   1 per mandare anche "nessuna novita'"

Uso
  python bot.py --once       esegue un giro e invia se c'e' qualcosa di nuovo
  python bot.py --dry-run    esegue un giro e stampa il messaggio, senza inviare
  python bot.py --test       invia un messaggio di prova
  python bot.py --chat-id    stampa l'id della chat da cui scrivere al bot
  python bot.py --loop       resta acceso: digest ogni giorno + comandi /oggi /start
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

TMDB_BASE = "https://api.themoviedb.org/3"
TG_BASE = "https://api.telegram.org/bot{token}/{method}"
GENERE_ANIMAZIONE = 16
USER_AGENT = "CinemaAnimazioneBot/1.0 (+python-urllib)"


# --------------------------------------------------------------------------
# configurazione
# --------------------------------------------------------------------------

def env(nome: str, default: str = "") -> str:
    return (os.environ.get(nome) or default).strip()


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TMDB_KEY = env("TMDB_API_KEY")
CHAT_ID = env("TELEGRAM_CHAT_ID", "8798296477")   # la tua chat
REGIONE = env("REGIONE", "IT")
LINGUA = env("LINGUA", "it-IT")
STATE_FILE = env("STATE_FILE", "stato.json")
INVIA_SE_VUOTO = env("INVIA_SE_VUOTO", "0") not in ("0", "", "no", "false")

def intero(nome: str, default: int) -> int:
    try:
        return int(env(nome, str(default)))
    except ValueError:
        return default


GIORNI_CINEMA = intero("GIORNI_CINEMA", 3)
VERSIONE_CINEMA = "v2:"      # chiave di stato: cambia se cambia il criterio
GIORNI_CATALOGO = intero("GIORNI_CATALOGO", 270)


def log(*parti) -> None:
    print(*parti, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _richiesta(url: str, dati: bytes | None = None, header: dict | None = None) -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if header:
        h.update(header)
    req = urllib.request.Request(url, data=dati, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        corpo = r.read().decode("utf-8", "replace")
    return json.loads(corpo)


def tmdb(percorso: str, parametri: dict | None = None) -> dict:
    """Chiamata GET a TMDB. Supporta chiave v3 (?api_key=) e token v4 (Bearer)."""
    if not TMDB_KEY:
        raise RuntimeError("Manca TMDB_API_KEY")
    parametri = dict(parametri or {})
    parametri.setdefault("language", LINGUA)
    url = f"{TMDB_BASE}{percorso}"
    header = {}
    if "." in TMDB_KEY:  # token v4 (JWT)
        header["Authorization"] = f"Bearer {TMDB_KEY}"
    else:
        parametri["api_key"] = TMDB_KEY
    url = f"{url}?{urllib.parse.urlencode(parametri)}"
    try:
        return _richiesta(url, header=header)
    except urllib.error.HTTPError as e:
        dettaglio = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"TMDB ha risposto {e.code}: {dettaglio}") from e


def telegram(metodo: str, **parametri):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Manca TELEGRAM_TOKEN")
    url = TG_BASE.format(token=TELEGRAM_TOKEN, method=metodo)
    dati = json.dumps(parametri).encode("utf-8")
    try:
        risposta = _richiesta(url, dati=dati, header={"Content-Type": "application/json"})
    except urllib.error.HTTPError as e:
        dettaglio = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Telegram ha risposto {e.code}: {dettaglio}") from e
    if not risposta.get("ok"):
        raise RuntimeError(f"Telegram: {risposta}")
    return risposta.get("result")


def invia(testo: str, chat_id: str | int) -> None:
    telegram(
        "sendMessage",
        chat_id=chat_id,
        text=testo,
        disable_web_page_preview=True,
    )


def trova_chat_id() -> str | None:
    """Ultima chat privata che ha scritto al bot (dopo /start)."""
    if CHAT_ID:
        return CHAT_ID
    try:
        aggiornamenti = telegram("getUpdates", timeout=0, limit=100) or []
    except RuntimeError as e:
        log("Impossibile leggere gli aggiornamenti:", e)
        return None
    for u in reversed(aggiornamenti):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"])
    return None


# --------------------------------------------------------------------------
# dati TMDB
# --------------------------------------------------------------------------

def film_al_cinema(oggi: date) -> list[dict]:
    """Film d'animazione GIA' usciti in sala in Italia negli ultimi giorni.
    Nessuna anteprima: chi non e' ancora in sala non viene segnalato."""
    dati = tmdb("/discover/movie", {
        "with_genres": GENERE_ANIMAZIONE,
        "region": REGIONE,
        "with_release_type": "2|3",       # 2 = sale limitate, 3 = sale
        "release_date.gte": (oggi - timedelta(days=GIORNI_CINEMA)).isoformat(),
        "release_date.lte": oggi.isoformat(),
        "sort_by": "popularity.desc",
        "page": 1,
    })
    return dati.get("results", [])


def film_in_streaming(oggi: date) -> list[dict]:
    """Film d'animazione disponibili in abbonamento su una piattaforma in Italia,
    ordinati dal più recente: i nuovi arrivi finiscono in cima."""
    dati = tmdb("/discover/movie", {
        "with_genres": GENERE_ANIMAZIONE,
        "watch_region": REGIONE,
        "with_watch_monetization_types": "flatrate",
        "sort_by": "primary_release_date.desc",
        "vote_count.gte": 5,
        "page": 1,
    })
    limite = (oggi - timedelta(days=GIORNI_CATALOGO)).isoformat()
    return [
        f for f in dati.get("results", [])
        if (f.get("release_date") or "") >= limite
    ]


def piattaforme(movie_id: int) -> tuple[list[str], str | None]:
    """Nomi delle piattaforme in abbonamento in Italia + link JustWatch."""
    try:
        dati = tmdb(f"/movie/{movie_id}/watch/providers")
    except RuntimeError as e:
        log(f"  provider per {movie_id} non disponibili: {e}")
        return [], None
    italia = (dati.get("results") or {}).get(REGIONE) or {}
    nomi: list[str] = []
    for voce in italia.get("flatrate") or []:
        nome = (voce.get("provider_name") or "").strip()
        if not nome:
            continue
        # "Netflix Amazon Channel" e simili sono doppioni: tengo solo il canale principale
        if nome.endswith("Amazon Channel"):
            continue
        if nome not in nomi:
            nomi.append(nome)
    return nomi, italia.get("link")


def data_italiana(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return iso
    mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    return f"{d.day} {mesi[d.month - 1]}"


# --------------------------------------------------------------------------
# stato (per non ripetere lo stesso titolo ogni giorno)
# --------------------------------------------------------------------------

def carica_stato() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            dati = json.load(f)
        if isinstance(dati, dict):
            dati.setdefault("cinema", {})
            dati.setdefault("streaming", {})
            return dati
    except (OSError, ValueError):
        pass
    return {"cinema": {}, "streaming": {}}


def salva_stato(stato: dict) -> None:
    limite = (date.today() - timedelta(days=90)).isoformat()
    for sezione in ("cinema", "streaming"):
        stato[sezione] = {
            k: v for k, v in stato[sezione].items()
            if isinstance(v, str) and v >= limite
        }
    temporaneo = STATE_FILE + ".tmp"
    with open(temporaneo, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporaneo, STATE_FILE)


# --------------------------------------------------------------------------
# composizione del messaggio
# --------------------------------------------------------------------------

def componi_messaggio(oggi: date, novo_cinema: list, novo_streaming: list) -> str:
    righe = [f"🎬 Novità animate — {data_italiana(oggi.isoformat())}", ""]
    if novo_cinema:
        righe.append("Al cinema")
        for voce in novo_cinema:
            titolo = voce["titolo"]
            quando = data_italiana(voce["data"])
            anno = f" ({voce['anno']})" if voce.get("anno") else ""
            if voce.get("data") == oggi.isoformat():
                righe.append(f"• {titolo}{anno} — da oggi")
            elif quando:
                righe.append(f"• {titolo}{anno} — dal {quando}")
            else:
                righe.append(f"• {titolo}{anno}")
        righe.append("")
    if novo_streaming:
        righe.append("In streaming")
        for voce in novo_streaming:
            titolo = voce["titolo"]
            anno = f" ({voce['anno']})" if voce.get("anno") else ""
            dove = ", ".join(voce["piattaforme"]) if voce["piattaforme"] else "streaming"
            righe.append(f"• {titolo}{anno} — su {dove}")
        righe.append("")
        righe.append("Streaming = disponibili in abbonamento adesso in Italia.")
    testo = "\n".join(righe).strip()
    while "\n\n\n" in testo:
        testo = testo.replace("\n\n\n", "\n\n")
    return testo


def prepara(oggi: date) -> tuple[str, dict, int, int]:
    """Raccoglie le novità, aggiorna lo stato e restituisce il messaggio."""
    stato = carica_stato()
    gia_visti_cinema = stato["cinema"]
    gia_visti_streaming = stato["streaming"]

    log("Cerco le uscite al cinema...")
    novo_cinema: list[dict] = []
    for film in film_al_cinema(oggi):
        mid = str(film.get("id"))
        if not mid or (VERSIONE_CINEMA + mid) in gia_visti_cinema:
            continue
        novo_cinema.append({
            "id": film.get("id"),
            "titolo": film.get("title") or film.get("original_title") or "?",
            "anno": (film.get("release_date") or "")[:4],
            "data": film.get("release_date") or "",
        })
    log(f"  {len(novo_cinema)} novità in sala")

    log("Cerco le novità in streaming...")
    novo_streaming: list[dict] = []
    candidati = [f for f in film_in_streaming(oggi) if str(f.get("id")) not in gia_visti_streaming]
    # segno come visti TUTTI i candidati di oggi: cosi' domani non ricompaiono a gruppi di 8
    for film in candidati:
        stato["streaming"][str(film.get("id"))] = oggi.isoformat()
    for film in candidati[:12]:                      # limite chiamate provider
        nomi, link = piattaforme(int(film["id"]))
        if not nomi:
            continue                                 # nessuna piattaforma in Italia
        novo_streaming.append({
            "id": film.get("id"),
            "titolo": film.get("title") or film.get("original_title") or "?",
            "anno": (film.get("release_date") or "")[:4],
            "piattaforme": nomi,
            "link": link,
        })
        if len(novo_streaming) >= 12:
            break
    log(f"  {len(novo_streaming)} novità in streaming")

    if not novo_cinema and not novo_streaming:
        return "", stato, 0, 0

    for voce in novo_cinema:
        stato["cinema"][VERSIONE_CINEMA + str(voce["id"])] = voce["data"] or oggi.isoformat()
    return componi_messaggio(oggi, novo_cinema, novo_streaming), stato, len(novo_cinema), len(novo_streaming)


# --------------------------------------------------------------------------
# comandi
# --------------------------------------------------------------------------

def comando_once(dry_run: bool = False) -> int:
    oggi = date.today()
    try:
        testo, stato, n_cinema, n_streaming = prepara(oggi)
    except RuntimeError as e:
        log("ERRORE:", e)
        return 1

    if not testo:
        log("Nessuna novità oggi.")
        if INVIA_SE_VUOTO:
            testo = f"🎬 Nessuna novità animata oggi ({data_italiana(oggi.isoformat())})."
        else:
            return 0

    if dry_run:
        log("---- MESSAGGIO (non inviato) ----")
        print(testo)
        log("---------------------------------")
        return 0

    chat = trova_chat_id()
    if not chat:
        log("Nessuna chat trovata: scrivi /start al bot una volta, poi riprova.")
        return 1
    invia(testo, chat)
    salva_stato(stato)
    log(f"Inviato a {chat}: {n_cinema} novità cinema, {n_streaming} streaming.")
    return 0


def comando_test() -> int:
    chat = trova_chat_id()
    if not chat:
        log("Nessuna chat trovata: apri il bot su Telegram e premi /start, poi riprova.")
        return 1
    invia("✅ Bot attivo. Da domani ti avviso quando esce un film d'animazione.", chat)
    log(f"Messaggio di prova inviato a {chat}.")
    return 0


def comando_chat_id() -> int:
    chat = trova_chat_id()
    if not chat:
        log("Nessuna chat trovata. Scrivi /start al bot e riprova.")
        return 1
    print(chat)
    return 0


def comando_loop() -> int:
    """Resta acceso: risponde ai comandi e manda il digest ogni giorno."""
    log("Bot in ascolto. Comandi: /start /oggi /help")
    offset = None
    ultimo_giorno = None
    while True:
        try:
            aggiornamenti = telegram("getUpdates", timeout=25, offset=offset) or []
        except RuntimeError as e:
            log("Errore di rete, riprovo:", e)
            time.sleep(5)
            aggiornamenti = []

        for u in aggiornamenti:
            offset = u.get("update_id", 0) + 1
            msg = u.get("message") or {}
            testo = (msg.get("text") or "").strip().lower()
            chat = (msg.get("chat") or {}).get("id")
            if not chat:
                continue
            if testo.startswith("/start"):
                invia("🎬 Ciao! Ti avviso quando esce un nuovo film d'animazione, al cinema o in streaming.", chat)
            elif testo.startswith("/oggi"):
                codice = comando_once()
                if codice:
                    invia("Oggi non riesco a leggere le uscite, riprovo più tardi.", chat)
            elif testo.startswith("/help"):
                invia("Comandi: /oggi per le novità adesso, /start per iniziare.", chat)

        ora = datetime.now()
        oggi = ora.date()
        if ora.hour >= intero("ORA", 9) and ultimo_giorno != oggi:
            ultimo_giorno = oggi
            try:
                chat = trova_chat_id()
                testo, stato, _, _ = prepara(oggi)
                if testo and chat:
                    invia(testo, chat)
                    salva_stato(stato)
                    log("Digest giornaliero inviato.")
                elif testo:
                    log("Digest pronto ma nessuna chat configurata.")
                else:
                    log("Nessuna novità oggi.")
            except RuntimeError as e:
                log("Errore nel digest:", e)
        time.sleep(1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Bot Telegram per le novità di film d'animazione")
    gruppo = parser.add_mutually_exclusive_group()
    gruppo.add_argument("--once", action="store_true", help="un giro e invio (default)")
    gruppo.add_argument("--dry-run", action="store_true", help="un giro senza inviare")
    gruppo.add_argument("--test", action="store_true", help="messaggio di prova")
    gruppo.add_argument("--chat-id", action="store_true", help="mostra l'id della chat")
    gruppo.add_argument("--loop", action="store_true", help="resta acceso con comandi e digest")
    args = parser.parse_args(argv)

    if not TELEGRAM_TOKEN:
        log("Manca TELEGRAM_TOKEN: impostalo nelle variabili d'ambiente.")
        return 2

    if args.test:
        return comando_test()
    if args.chat_id:
        return comando_chat_id()
    if args.loop:
        return comando_loop()
    return comando_once(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
