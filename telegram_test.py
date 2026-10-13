"""Invia un solo messaggio di prova Telegram; nessuna allerta meteo reale."""
import json
import os
import sys
import urllib.error
import urllib.request

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not token or not chat:
    print("Configurazione incompleta: inserire TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID nei repository secrets.")
    sys.exit(1)

payload = json.dumps({
    "chat_id": chat,
    "text": "TEST Radar Meteo Conversano: le notifiche Telegram funzionano. Questo NON e' un allarme meteorologico.",
    "disable_web_page_preview": True,
}).encode("utf-8")
request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/sendMessage",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if not data.get("ok"):
        print("Telegram non ha confermato la consegna del messaggio.")
        sys.exit(1)
    print("Test concluso: Telegram ha accettato il messaggio. Controlla la chat del bot.")
except urllib.error.HTTPError as error:
    print(f"Invio non riuscito: Telegram ha restituito HTTP {error.code}.")
    sys.exit(1)
except Exception:
    # Non stampare URL o eccezioni: contengono potenzialmente il token.
    print("Invio non riuscito: errore di connessione o risposta non valida.")
    sys.exit(1)
