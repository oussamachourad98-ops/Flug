# Flug – persönlicher Flugpreis-Monitor

Durchsucht regelmäßig echte Flugangebote von **mehreren Abflughäfen** zu einem oder mehreren Zielen mit
**flexiblen Daten**, speichert die Preishistorie, vergleicht Gesamtpreise und schickt **Telegram-Alarme**,
wenn ein Zielpreis unterschritten oder ein neuer Tiefstpreis gefunden wird. Ein kleines Web-Dashboard
zeigt die günstigsten Angebote und den Preisverlauf.

## Flug-APIs: Stand November 2026

| Anbieter | Status | Bewertung |
|---|---|---|
| **Amadeus Self-Service** (Flight Offers Search) | **Am 17.07.2026 abgeschaltet.** Neue Registrierungen wurden schon vorher gestoppt; es gibt nur noch Amadeus Enterprise (Vertrag, meist IATA/ARC-Akkreditierung). | ❌ nicht verwendet |
| **Kiwi.com Tequila** | Seit 2024 kein Self-Service mehr, nur auf Einladung für Partner. | ❌ nicht verwendet |
| **Skyscanner Partner API** | Nur nach kommerzieller Freigabe (hohe Reichweite nötig). | ❌ nicht verwendet |
| **Duffel Flights API (v2)** | Aktiv, Self-Service. Live-Angebote direkt von Airlines (NDC/GDS) mit Gepäck, Tarifbedingungen und Ablaufzeit; über die API buchbar. | ✅ **Hauptquelle** |
| **SerpApi – Google Flights** | Aktiv, kostenpflichtig mit kleinem Gratiskontingent. Liefert echte Google-Flights-Ergebnisse plus Link auf genau diese Google-Flights-Suche. | ✅ optionale zweite Quelle |

### Einschränkungen, die du kennen solltest

**Duffel**
- Test-Tokens (`duffel_test_…`) liefern nur die fiktive „Duffel Airways“. **Echte Preise gibt es nur mit einem Live-Token** (`duffel_live_…`); dafür muss das Duffel-Konto verifiziert sein.
- Suchen sind kostenlos, solange das Verhältnis Suchen:Buchungen ≤ 1500:1 bleibt (null Buchungen zählen als eine).
  Darüber kostet jede Suche **0,005 USD**. Deshalb hat die App ein Monatslimit (`DUFFEL_MONTHLY_SEARCH_LIMIT`, Standard 1500).
- Duffel hat **keinen öffentlichen Buchungslink pro Angebot**: Angebote werden über die API (`POST /air/orders`) gebucht.
  Die App verlinkt deshalb eine **Google-Flights-Suche mit gleichen Flughäfen und Daten** (im Dashboard als „Suche öffnen“ markiert, Preis dort kann abweichen).
  Die Duffel-Angebots-ID wird gespeichert, damit du das Angebot vor Ablauf (`expires_at`, meist ~30 min) per API buchen kannst.
- Duffel liefert Preise in der Währung der Airline/des Kontos. Angebote in anderer Währung als die der Route werden gespeichert, aber nicht mit dem Zielpreis verglichen.
- Gepäck: Duffel gibt pro Segment die *inkludierten* Aufgabe-/Handgepäckstücke an. Fehlt die Angabe, zeigt die App „keine Angabe“ (nicht „0“).

**SerpApi (Google Flights)**
- Bei Hin- und Rückflug liefert Google Flights den Preis für die ganze Reise, aber Details nur zum **Hinflug** (in Alarmen markiert).
- Gepäck nur, wenn Google es als Hinweis mitliefert. Der Link öffnet die Google-Flights-Seite **genau dieser Suche**, nicht ein einzelnes Angebot.
- Jede Suche verbraucht eine SerpApi-Suche aus deinem Kontingent (`SERPAPI_MONTHLY_SEARCH_LIMIT`). Prüfe dein Kontingent unter serpapi.com/manage-api-key.

## Rechne dein Suchvolumen aus

Suchen pro Runde = Abflughäfen × Ziele × Datumskombinationen × Anbieter.
Beispiel: 3 Abflughäfen × 1 Ziel × 4 Datumspaare = 12 Suchen. Alle 30 Minuten sind das **~17.000 Suchen im Monat**:
weit über dem kostenlosen Duffel-Rahmen (1500) und ~85 USD Excess-Gebühr.

```bash
flug check-config     # zeigt Suchen pro Runde und pro Monat und warnt bei Überschreitung
```

Ein realistischer Gratis-Betrieb: wenige Kombinationen und ein Intervall von 3–6 h (`SEARCH_INTERVAL_MINUTES=240`).
Ist das Monatslimit erreicht, sucht die App nicht weiter. Das Dashboard zeigt den Zähler.

## Einrichtung (lokal)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                    # Tokens eintragen
cp watches.example.yaml watches.yaml    # Routen anpassen
flug places "Düsseldorf"               # IATA-Codes nachschlagen (Duffel)
flug check-config
flug test-telegram
flug search-once                        # eine Suchrunde
flug serve                              # Dashboard (http://localhost:8000) + Scheduler
pytest -q
```

### API-Zugänge

1. **Duffel**: Konto auf app.duffel.com anlegen → *Developers → Access tokens*. Zum Ausprobieren ein Test-Token,
   für echte Preise Live-Modus aktivieren (Kontoverifizierung) und ein **Live-Token mit Leserechten** erstellen.
2. **SerpApi** (optional): Konto auf serpapi.com → API-Key. `PROVIDERS=duffel,serpapi` setzen.
3. **Telegram**: Bei @BotFather `/newbot` → Token. Dem Bot eine Nachricht schreiben, dann
   `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen und `chat.id` ablesen.

### Routen (`watches.yaml`)

Siehe `watches.example.yaml`. Pro Route: `origins` (mehrere Flughäfen oder Stadtcodes wie `BER`, `LON`),
`destinations`, `departure` (Bereich `{from, to}` oder Liste), optional `return` **oder** `stay_nights`
(ohne beides = One-way), `target_price`, `currency`, `adults`, `cabin_class`, `max_connections`, `max_date_combinations`.
Der Zielpreis lässt sich auch im Dashboard ändern. Eine spätere Änderung in der YAML gewinnt.

## Betrieb in der Cloud / auf einem VPS (läuft ohne Laptop)

Empfohlen: kleiner VPS (z. B. 1 vCPU / 1 GB) mit Docker.

```bash
git clone <repo> flug && cd flug
cp .env.example .env && nano .env            # Tokens + DASHBOARD_PASSWORD
cp watches.example.yaml watches.yaml && nano watches.yaml
docker compose up -d --build
docker compose logs -f
docker compose exec flug flug test-telegram
```

- Container startet nach Reboots neu (`restart: unless-stopped`). Die Datenbank liegt in `./data/flug.db`.
- Das Dashboard ist nur an `127.0.0.1:8000` gebunden. Zugriff per SSH-Tunnel (`ssh -L 8000:127.0.0.1:8000 user@vps`)
  oder hinter einen Reverse-Proxy mit HTTPS stellen (z. B. Caddy: `flug.example.com { reverse_proxy 127.0.0.1:8000 }`).
- Für PostgreSQL `DATABASE_URL=postgresql+psycopg://…` setzen.
- Alternative ohne Dauerprozess: `flug search-once` per Cron/systemd-Timer und das Dashboard separat.

## Alarme & Duplikate

Pro Lauf und Route:
- **Unter Zielpreis**: bis zu 3 verschiedene Angebote ≤ Zielpreis (günstigste zuerst).
- **Neuer Tiefstpreis**: günstigstes Angebot liegt unter allen bisher gespeicherten Preisen dieser Route (gleiche Währung).
- **Keine Doppel-Alarme**: Ein Angebot wird über einen Fingerabdruck erkannt (Anbieter, Strecke, Daten, Flugnummern, Abflugzeiten).
  Es wird erneut gemeldet, wenn es mindestens `ALERT_MIN_DROP` günstiger ist als beim letzten Alarm.
  Schlägt die Telegram-Zustellung fehl, wird der Alarm im nächsten Lauf erneut versucht.

## Sicherheit

- Secrets nur in `.env` oder Umgebungsvariablen. `.env` und `watches.yaml` stehen in `.gitignore`.
- Dashboard nur mit HTTP Basic Auth (Vergleich in konstanter Zeit). Ohne `DASHBOARD_PASSWORD` startet es nicht
  (Ausnahme: `DASHBOARD_ALLOW_NO_AUTH=true` für reine Lokalnutzung).
- Security-Header (CSP, X-Frame-Options, nosniff). Same-Origin-Prüfung beim Formular. Chart.js wird lokal ausgeliefert, kein CDN.
- `httpx`-Request-Logs sind abgeschaltet, damit der Telegram-Token (Teil der URL) nicht im Log landet.
- Container läuft als Nicht-Root-User. Port nur auf localhost.
- Für Duffel reicht ein Token mit Leserechten.

## Fehlerbehandlung

- HTTP-Retries mit exponentiellem Backoff bei Timeouts, 429 (mit `Retry-After`) und 5xx. Bei 401/403 kein Retry:
  der Anbieter wird für den Lauf deaktiviert und der Fehler gespeichert.
- Eine fehlerhafte Suche oder ein nicht lesbares Angebot bricht den Lauf nicht ab (Status `partial`).
- Eine abstürzende Route stoppt die anderen nicht. Überlappende Läufe werden übersprungen.
- Vergangene Daten werden übersprungen. Eine Route mit nur noch vergangenen Daten bekommt den Status `skipped`.
- `/healthz` für Monitoring (Docker-Healthcheck ist eingebaut).

## Tests

`pytest -q`: Datumslogik, Duffel- und SerpApi-Parsing gegen Beispielantworten im Format der Anbieter (inkl. Retries und Auth-Fehlern),
Alarm-/Duplikatlogik, Budget-Limit, Dashboard (Auth, CSRF, Zielpreis) und Telegram. Die Tests machen **keine** echten API-Aufrufe.
Ob die Preise echt sind, prüfst du einmalig mit einem Live-Token: `flug search-once`, dann im Dashboard die
Angebote mit der verlinkten Google-Flights-Suche vergleichen. Im Log warnt die App ausdrücklich, wenn ein Test-Token aktiv ist.
