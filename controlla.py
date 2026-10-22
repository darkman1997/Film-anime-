"""
Controlla i film di animazione e ti scrive su Telegram.
Pensato per girare su GitHub Actions: parte, controlla, invia e si spegne.

Variabili d'ambiente (segreti di GitHub):
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TMDB_API_KEY
"""

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TMDB_KEY = os.environ["TMDB_API_KEY"]

REGION = "IT"
LANG = "it-IT"
GENERE_ANIMAZIONE = 16
# ID provider TMDB: 8 = Netflix, 119 = Prime Video, 337 = Disney+
PROVIDER_STREAMING = "8|119|337"
STATE_FILE = "stato.json"


def carica_stato() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"cinema": [], "streaming": None}


def salva_stato(stato: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=2)


def discover(extra: dict) -> list[dict]:
    params = {
        "api_key": TMDB_KEY,
        "language": LANG,
        "with_genres": GENERE_ANIMAZIONE,
        "include_adult": "false",
        **extra,
    }
    r = requests.get("https://api.themoviedb.org/3/discover/movie", params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])


def film_al_cinema_oggi() -> list[dict]:
    oggi = dt.datetime.now(ZoneInfo("Europe/Rome")).date().isoformat()
    return discover({
        "region": REGION,
        "with_release_type": "2|3",  # 2 = limitata, 3 = cinema
        "release_date.gte": oggi,
        "release_date.lte": oggi,
    })


def film_in_streaming() -> list[dict]:
    return discover({
        "watch_region": REGION,
        "with_watch_providers": PROVIDER_STREAMING,
        "with_watch_monetization_types": "flatrate",
        "sort_by": "primary_release_date.desc",
    })


def formatta(film: dict, etichetta: str) -> str:
    trama = (film.get("overview") or "Trama non disponibile.").strip()
    if len(trama) > 350:
        trama = trama[:350].rstrip() + "…"
    data = film.get("release_date") or "data da definire"
    return (
        f"🎬 {etichetta}\n"
        f"{film.get('title', 'Senza titolo')} ({data})\n\n"
        f"{trama}\n\n"
        f"https://www.themoviedb.org/movie/{film['id']}"
    )


def invia(testo: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": testo},
        timeout=20,
    )
    r.raise_for_status()


def main() -> None:
    stato = carica_stato()

    # Cinema: solo i film che escono oggi, mai segnalati due volte.
    visti_cinema = set(stato.get("cinema", []))
    for f in film_al_cinema_oggi():
        if f["id"] not in visti_cinema:
            invia(formatta(f, "Esce oggi al cinema"))
            visti_cinema.add(f["id"])
    stato["cinema"] = sorted(visti_cinema)

    # Streaming: al primo avvio memorizzo l'elenco attuale senza inviare nulla.
    film = film_in_streaming()
    ids_attuali = [f["id"] for f in film]
    visti_streaming = stato.get("streaming")
    if visti_streaming is None:
        stato["streaming"] = ids_attuali
    else:
        for f in film:
            if f["id"] not in visti_streaming:
                invia(formatta(f, "Disponibile in streaming"))
        stato["streaming"] = sorted(set(visti_streaming) | set(ids_attuali))

    salva_stato(stato)


if __name__ == "__main__":
    main()
