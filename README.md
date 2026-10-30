# Nowcasting radar Conversano — PGL320

Progetto sperimentale per osservare le precipitazioni stimate dal radar della Protezione Civile intorno alla stazione meteorologica **PGL320** (Conversano-San Vito, coordinate 40.976 N, 17.13 E). Non è un sistema ufficiale di allerta e non garantisce l'anticipo sull'arrivo della pioggia.

## Stato: monitoraggio programmato sperimentale

**Avvio regolare ogni 10 minuti da Google Apps Script** (evento `workflow_dispatch`, script in `apps_script/RadarDispatch.gs`), perché la pianificazione cron di GitHub nella pratica parte con 20-60 minuti di ritardo. Il cron del workflow (minuti 09, 19, 29, 39, 49 e 59 UTC) resta come riserva. Ogni esecuzione elabora tutte le scansioni SRI pubblicate dopo l'ultima già elaborata (fino a 12, cioè 60 minuti), quindi un'esecuzione saltata o in ritardo non fa perdere scansioni e le esecuzioni doppie non producono avvisi doppi. Lo stesso script controlla una volta l'ora che il workflow sia attivo e abbia girato di recente, e in caso contrario avvisa su Telegram.

Il workflow di sorveglianza si trova in `.github/workflows/radar-watch.yml`: legge due scansioni SRI, invia messaggi **solo se** trova un episodio che raggiunge le soglie sperimentali e aggiorna lo stato dell'episodio in `state/stato_avvisi.json`. Il registro di stato è pubblico (timestamp e livelli, non segreti). Gli altri workflow servono per i test:

- **Radar Conversano - prova manuale**: legge due scansioni radar senza inviare notifiche.
- **Radar avvisi - verifica manuale**: esegue test di gestione degli episodi e simulazione senza invio.
- **Telegram - messaggio di prova**: invia *un solo* messaggio di prova quando sono configurati i segreti.

La lettura programmata e gli avvisi radar sono stati configurati dopo il superamento di otto test automatici e di una lettura radar di prova; verificare le prime esecuzioni pianificate nella pagina Actions.

## Funzionamento sperimentale

`nowcasting.py` scarica due scansioni consecutive del prodotto radar **SRI** tramite API della Protezione Civile, verifica timestamp e copertura, e valuta le intensità in mm/h sulla cella della PGL320 e su aree fino a 40 km. Gestisce esplicitamente i valori privi di dato.

Le soglie locali attuali, ancora da calibrare, distinguono pioggia entro 5 km e precipitazioni intense entro 10 km confermate in due scansioni radar. **Il dato SRI stima l'intensità della precipitazione, non misura l'accumulo al suolo, non rileva i fulmini e non dimostra la presenza di un temporale.**

`alert_runner.py` gestisce lo stato degli episodi con lettura/scrittura del file `state/stato_avvisi.json` via GitHub API, così da evitare avvisi ripetuti nella normale operatività. La procedura non garantisce l'assenza assoluta di duplicati in caso di errori/interruzioni durante l'invio.

## Sicurezza e privacy

- Il codice e la cronologia di **questo** repository possono essere resi pubblici, mentre il repository originale di sviluppo rimane privato.
- Non pubblicare mai il token del bot Telegram, il numero `chat_id`, password, URL di API con token o variabili d'ambiente contenenti credenziali.
- I valori `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` devono essere inseriti come **GitHub Actions repository secrets** nelle impostazioni di questo repository, e non vengono copiati dall'altro repository.
- I workflow non eseguono codice di pull request esterne e non concedono accesso ai segreti a contributori esterni.
- La posizione della PGL320 è dichiarata esplicitamente e sarà visibile pubblicamente.
- Le esecuzioni programmate GitHub Actions possono subire ritardi o essere saltate; il sistema non sostituisce le comunicazioni ufficiali della Protezione Civile.

## Prove manuali

In GitHub apri **Actions**, seleziona il workflow da collaudare, poi **Run workflow** sul ramo `main`. Per provare il radar senza invio di notifiche esegui prima **Radar avvisi - verifica manuale** e controlla l'esito dei test.

## Limiti operativi e manutenzione

- GitHub esegue normalmente una verifica ogni 10 minuti, ma le attività pianificate possono essere ritardate o saltate.
- Il radar SRI ha una latenza variabile rispetto alla scansione osservata: non è garantito un anticipo sull'inizio della pioggia.
- Gli avvisi indicano **pioggia stimata nelle vicinanze**, non necessariamente precipitazioni in arrivo sulla PGL320, né presenza di fulmini.
- Le soglie sono sperimentali e possono produrre falsi avvisi o mancare eventi; non sostituiscono le allerte ufficiali.
- In caso di perdita di connettività o dati radar scaduti il job segnala un errore nei log e nel registro `registro/AAAA/AAAA-MM-GG.csv`; il messaggio di guasto su Telegram arriva dallo script Apps Script di sorveglianza, se configurato.
- I workflow pianificati dei repository pubblici GitHub sono disattivati automaticamente dopo 60 giorni senza attività del repository; verificare periodicamente il pannello Actions.
- I runner standard nei repository pubblici sono gratuiti secondo la documentazione GitHub; possibili vincoli diversi per runner a pagamento, storage o altre risorse.
- Per disattivare: aprire **Actions → PGL320 - sorveglianza radar automatica → Disable workflow**.
- Nel caso raro in cui Telegram confermi un invio e il successivo salvataggio dello stato fallisca, può ripetersi una notifica; il sistema non garantisce consegna e deduplicazione assolute.


Licenza: nessuna concessione generale di riutilizzo dichiarata in questa fase.
