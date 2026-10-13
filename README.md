# Nowcasting radar Conversano — PGL320

Progetto sperimentale per osservare le precipitazioni stimate dal radar della Protezione Civile intorno alla stazione meteorologica **PGL320** (Conversano-San Vito, coordinate 40.976 N, 17.13 E). Non è un sistema ufficiale di allerta e non garantisce l'anticipo sull'arrivo della pioggia.

## Stato: solo test manuali

**Nessun monitoraggio programmato è attivo.** Tutti i workflow presenti sono attivabili esclusivamente a mano con `workflow_dispatch`:

- **Radar Conversano - prova manuale**: legge due scansioni radar senza inviare notifiche.
- **Radar avvisi - verifica manuale**: esegue test di gestione degli episodi e simulazione senza invio.
- **Telegram - messaggio di prova**: invia *un solo* messaggio di prova quando sono configurati i segreti.

Il monitoraggio automatico e le notifiche radar non sono ancora attivi: saranno configurati solo dopo i collaudi.

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

## Prima dell'attivazione automatica

Vanno ancora eseguiti i test nel nuovo repository e verificati i segreti Telegram, i permessi di scrittura necessari allo stato persistente, le condizioni di concorrenza dei job e gli effetti della frequenza dei controlli. Prima di aggiungere uno schedule occorre verificarne limiti, qualità dei dati e latenza effettiva.

Licenza: nessuna concessione generale di riutilizzo dichiarata in questa fase.
