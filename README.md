# MietRadar 📡
### Automated Apartment & WG Application Bot for Germany — WG-Gesucht + ImmobilienScout24

> MietRadar monitors [WG-Gesucht](https://www.wg-gesucht.de) and [ImmobilienScout24](https://www.immobilienscout24.de). The Munich radar picks out fresh WG-Gesucht listings and prepares application drafts for review. The original two bots are documented below.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL](https://img.shields.io/badge/license-GPL-green.svg)](LICENSE)
[![Platforms](https://img.shields.io/badge/platforms-WG--Gesucht%20%7C%20ImmoScout24-orange.svg)](#features)

---

## München-Radar

Der Radar sucht WG-Zimmer, Einzimmerwohnungen und Wohnungen in München auf WG-Gesucht und optional über Suchalarme von Kleinanzeigen. Er sortiert passende Entwürfe nach Datum. Standardmäßig gilt ein Zeitfenster von 24 Stunden. Bei Kleinanzeigen zählt der Eingang des Suchalarms im Postfach.

- **Lage:** Für Liste A dauert die Fahrt von der Adresse zum Hauptbahnhof **oder** zum Ostbahnhof höchstens 30 Minuten. Für weitere Angebote prüft der Radar die Gegenrichtung: B bis 45 Minuten **ab Ostbahnhof**, C bis 45 Minuten **ab Hauptbahnhof**. B hat Vorrang, wenn beide Verbindungen passen. Die Fahrzeit enthält Fußwege und Wartezeit ab 08:00 Uhr am nächsten Werktag (Mo–Fr) Münchner Zeit. Die Listen B und C stehen im Dashboard zur Durchsicht.
- **WG:** Klar formulierte Gesuche nur für Studierende oder nur für Frauen werden aussortiert. Liegt ein genanntes Alter der Mitbewohnenden außerhalb von **21 bis 35 einschließlich**, wird die Anzeige übersprungen. Fehlende Altersangaben werden akzeptiert. Der Textfilter erfasst gängige Formulierungen; prüf die Anzeige vor dem Abschicken noch einmal.
- **Bewerbung:** Standardmäßig speichert der Radar Entwürfe für dich. Du kannst ihn auch automatisch verschicken lassen. Für personalisierte Absätze kannst du Mistral Studio hinzufügen: Das ist die API von Mistral, die unabhängig vom Vibe-Chatkonto eingerichtet wird. Dein persönlicher Bewerbungstext bleibt in deiner Vorlage.

### Lokal starten

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-radar.txt
cp config/.env.example config/.env
cp config/message.txt.example config/message.txt
```

1. Ersetze den Beispieltext in `config/message.txt` durch deine eigenen Angaben. Mit `{LLM_TEXT}` legst du fest, wo ein kurzer persönlicher Absatz stehen soll.
2. Die ÖPNV-Prüfung nutzt [Transitous](https://transitous.org/) mit [MOTIS](https://github.com/motis-project/motis) und Geodaten von [OpenStreetMap](https://www.openstreetmap.org/copyright). Der öffentliche Dienst braucht keinen API-Schlüssel. Die Fahrpläne stammen aus den [Transitous-Datenquellen](https://transitous.org/sources/). Stimme einen dauerhaften automatisierten Betrieb mit Transitous ab oder setze `MUNICH_TRANSIT_API_URL` auf eine eigene MOTIS-Instanz. [Nutzungsbedingungen für die öffentliche API](https://transitous.org/api/)
3. Wähle in `config/.env` eine Quelle. **`MUNICH_SOURCE=alerts`** liest Suchalarm-Mails von WG-Gesucht. Richte dafür auf der Plattform einen [Suchauftrag mit E-Mail-Benachrichtigung](https://www.wg-gesucht.de/hilfebereich.html) ein und leite die passenden Mails per Postfachregel in einen eigenen Ordner. Setze `MUNICH_IMAP_HOST`, `MUNICH_IMAP_USER`, `MUNICH_IMAP_PASSWORD`, `MUNICH_IMAP_FOLDER` und `MUNICH_ALERT_SENDER` (die genaue Absenderadresse). Der Radar liest den Ordner per IMAP über TLS. Wenn das Veröffentlichungsdatum vorliegt, gilt dieses; sonst zählt der Eingang des Suchalarms. **`MUNICH_SOURCE=public`** liest die öffentlichen Suchseiten. Mit `MUNICH_SEARCH_URLS` kannst du eigene WG-Gesucht-Suchergebnislinks hinterlegen.
4. Optional: Setze `MUNICH_LLM_PROVIDER=mistral` und `MISTRAL_API_KEY` aus [Mistral Studio](https://docs.mistral.ai/api/endpoint/chat). `MISTRAL_MODEL` ist optional (`mistral-small-latest`). Sonst nutzt der Radar die Vorlage direkt.

```bash
python src/munich_radar.py scan          # einmal suchen
python src/munich_radar.py watch         # sofort und danach stündlich suchen
python src/munich_radar.py drafts        # aktuelle Treffer, neueste zuerst
python src/munich_radar.py drafts 123456 # Bewerbungstext zu einer Anzeige anzeigen
python src/munich_radar.py done 123456   # nach dem Abschicken oder Verwerfen ausblenden
```

Im öffentlichen Modus erfasst der Radar je Suche standardmäßig zwei Ergebnisseiten (`MUNICH_WG_SEARCH_PAGES`) und prüft pro Lauf höchstens 24 Detailseiten (`MUNICH_MAX_CANDIDATES`). Die SQLite-Datenbank merkt sich, wann sie eine Anzeigen-ID zuerst und zuletzt gesehen hat. Der erste vollständige Lauf erfasst den Bestand. Ist ein zuvor nicht erfasstes Inserat bei einem Folgelauf zu sehen, kann es ohne öffentliches Veröffentlichungsdatum als **Zuerst gesehen** in die Entwürfe kommen. Dieser Zeitpunkt beschreibt die Beobachtung durch MietRadar. Für den automatischen Versand reicht er nicht. Auf Railway bleibt die Historie auf dem `/data`-Volume erhalten.

### Dashboard öffnen

Das Streamlit-Dashboard zeigt neue passende Angebote, Bewerbungsentwürfe, offene Adressprüfungen, Antwortentwürfe und den Verlauf. Du kannst den Text ansehen und kopieren. Das Dashboard liest die SQLite-Datenbank des Workers.

Unter **Angebote** kannst du das letzte Wochenende oder eigene Kalendertage in Münchner Zeit auswählen. **Veröffentlicht** zeigt Anzeigen mit einem belegten Veröffentlichungsdatum. **Zuerst gesehen** zeigt dagegen, wann MietRadar Anzeigen ohne belegtes Veröffentlichungsdatum erstmals erfasst hat. Die Datumsansicht zeigt auch erfasste Anzeigen ohne Entwurf. Ein Veröffentlichungsdatum ohne Uhrzeit reicht für die Datumsansicht. Der Radar erstellt Bewerbungsentwürfe und versendet Bewerbungen ausschließlich für Liste A.

Lokal: Installiere die Abhängigkeiten mit `pip install -r requirements-dashboard.txt` und hinterlege ein eigenes langes Passwort als `MUNICH_DASHBOARD_PASSWORD` in deiner privaten `config/.env`. Starte den Worker und öffne das Dashboard in einem zweiten Terminal:

```bash
python -m streamlit run src/dashboard.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Öffne `http://127.0.0.1:8501` und melde dich mit dem Passwort an. Ein neuer Browser-Tab braucht eine eigene Anmeldung.

Auf Railway: Setze `MUNICH_DASHBOARD_ENABLED=true` und `MUNICH_DASHBOARD_PASSWORD` in **Variables**. Der Docker-Startbefehl startet den Worker und das Dashboard gemeinsam, damit beide dasselbe `/data`-Volume nutzen. Erstelle danach unter **Settings → Networking** eine HTTPS-Domain für diesen Dienst. Mit einer eigenen Domain kannst du ihren Hostnamen ohne `https://` unter `MUNICH_DASHBOARD_HOST` setzen. Die Anmeldung schützt die Entwürfe und Antworten hinter dieser URL. [Railway-Netzwerk einrichten](https://docs.railway.com/networking/public-networking)

### Kleinanzeigen ergänzen

Speichere auf Kleinanzeigen eine Suche für Münchner Mietwohnungen oder Auf Zeit & WG mit der Angebotsart **Angebote** und aktiviere die [E-Mail-Suchbenachrichtigung](https://hilfe.kleinanzeigen.de/hc/de/articles/17102975598492-Ich-m%C3%B6chte-automatisch-neue-Suchergebnisse-zu-meiner-Suche-erhalten-Wie-richte-ich-das-ein). Sortiere diese Mails per Postfachregel in einen separaten IMAP-Ordner. Setze `KLEINANZEIGEN_IMAP_FOLDER` und `KLEINANZEIGEN_ALERT_SENDER` auf diesen Ordner und die genaue Absenderadresse. Die Zugangsdaten kommen aus `MUNICH_IMAP_HOST`, `MUNICH_IMAP_USER` und `MUNICH_IMAP_PASSWORD`. Die gleichen Variablen gelten auf Railway.

Bei einem eigenen Postfach für Suchalarme, etwa [mailbox.org](https://kb.mailbox.org/en/private/e-mail/e-mail-configuration/), kannst du `INBOX` direkt verwenden. Setze bei mailbox.org `MUNICH_IMAP_HOST=imap.mailbox.org`, den vollständigen Postfachnamen als Benutzer und ein [App-Passwort nur für IMAP](https://kb.mailbox.org/en/business/security-and-privacy/e-mail-password/). Kopiere den tatsächlichen Absender aus einem angekommenen Suchalarm.

Der Radar verarbeitet direkte Inseratslinks aus den Suchalarm-Mails. Enthält der Alarm eine vollständige Münchner Adresse, vergibt er dieselben ÖPNV-Bewertungen wie bei WG-Gesucht. Mit einer PLZ, einem Viertel oder einem [ungefähren Kartenpunkt](https://hilfe.kleinanzeigen.de/hc/de/articles/29128094755100-Neu-Immobilien-auf-der-Karte-ansehen) bleibt der Treffer zur Ortsprüfung offen. Wenn du im Inserat eine genaue Adresse findest, kannst du sie selbst prüfen lassen.

```bash
python src/munich_radar.py klein-review
python src/munich_radar.py klein-address ka-1234567890 --address 'Musterstraße 12, 81667 München'
python src/munich_radar.py drafts ka-1234567890
```

Bei Kleinanzeigen kann der Radar Entwürfe bereithalten oder sie automatisch über das normale Kontaktformular verschicken. Angaben zum WG-Profil können nur geprüft werden, soweit sie in der Suchalarm-Mail stehen.

### Automatisch bewerben

1. Installiere die Browser-Abhängigkeiten (`pip install -r requirements-send.txt`) und Chrome oder Chromium. Füll zuerst deine eigene deutsche Bewerbungsvorlage aus.
2. Setze `WG_EMAIL`, `WG_PASSWORD` und `MUNICH_AUTOSEND=true` in `config/.env`. Starte `python src/munich_radar.py watch`. Auch `scan` verschickt Bewerbungen, wenn die Option gesetzt ist.
3. Mit `python src/munich_radar.py delivery` siehst du den Versandstatus. `MUNICH_AUTOSEND=false` schaltet wieder auf Entwürfe um.

Der Versand verarbeitet die neuesten Treffer zuerst. Direkt vor dem Klick gilt eine feste Obergrenze von **24 Stunden**. Eine kürzere Einstellung aus `MUNICH_MAX_AGE_HOURS` gilt weiterhin. Bei Suchalarmen kann der Maileingang als Aktualitätssignal dienen, wenn das Veröffentlichungsdatum fehlt. Standardmäßig sind insgesamt höchstens drei Versandversuche je 24 Stunden erlaubt (`MUNICH_DAILY_SEND_LIMIT`, 1–5).

Der München-Radar prüft Bewerbungsentwürfe beim Speichern sowie jede automatische Bewerbung und Antwort vor dem Versand. Standardmäßig prüft er lokal auf offene Platzhalter, vertrauliche Angaben und riskante Zusagen. Mit `MUNICH_OUTGOING_JUDGE_PROVIDER=mistral` kommt eine Mistral-Prüfung hinzu. Nur eine ausdrückliche Freigabe erlaubt den Versand. Bei einem API-Fehler bleibt die Nachricht liegen. Die Gründe stehen im Dashboard und unter `python src/munich_radar.py outgoing-checks`.

Wenn WG-Gesucht eine zusätzliche Prüfung anzeigt, pausiert der Versand. Entwürfe werden weiter erstellt. Bei einem unklaren Ergebnis wird dieselbe Anzeige nicht erneut automatisch angeschrieben; prüf zuerst das WG-Gesucht-Postfach. Starte den Worker neu, sobald das Login oder die Prüfung geklärt ist. Verwende für den München-Radar und den alten WG-Gesucht-Bot keinen gleichzeitigen Versand auf demselben Konto.

#### Automatischer Kleinanzeigen-Versand

1. Melde dich einmal selbst bei Kleinanzeigen in einem separaten Chrome- oder Chromium-Profil an. Der Radar erwartet das Browserprofil unter `data/kleinanzeigen_browser` (relativ zum Repo) oder unter `$MUNICH_DATA_DIR/kleinanzeigen_browser`. Schließe das Profil vor dem Worker-Start. Auf macOS kannst du es so öffnen:

   ```bash
   open -na 'Google Chrome' --args --user-data-dir="$PWD/data/kleinanzeigen_browser" https://www.kleinanzeigen.de/m-einloggen.html
   ```

2. Setze `KLEINANZEIGEN_AUTOSEND=true` und starte `watch`. Die Kleinanzeigen-Suchalarme müssen konfiguriert sein. Ein Entwurf aus `klein-address` kann im nächsten Lauf verschickt werden, solange der selbst eingetragene Standort die ÖPNV-Prüfung erneut besteht und die Anzeige noch frisch ist.
3. Prüfe `python src/munich_radar.py delivery`. Der Radar markiert einen Versand als bestätigt, wenn er die gesendete Nachricht im Nachrichtenverlauf erkennt. Bei Anmeldung, Verifikation oder unklarem Ergebnis pausiert der Versand. Unklare Versuche werden nicht automatisch wiederholt.

#### Antworten sortieren und Hinweise bekommen

Aktiviere in Kleinanzeigen unter **Meins → Einstellungen → E-Mails** die Option **Nachrichten von Nutzern**. Lege für Kopien deiner Wohnungsgespräche einen separaten IMAP-Ordner an und setze `KLEINANZEIGEN_REPLIES_IMAP_FOLDER` und `KLEINANZEIGEN_REPLY_SENDER` auf diesen Ordner und die genaue Absenderadresse der Nachrichten-Kopien. Der Radar liest diesen Ordner mit denselben IMAP-Zugangsdaten wie die Suchalarme. [Kleinanzeigen-E-Mail-Einstellungen](https://hilfe.kleinanzeigen.de/hc/de/articles/17145751363996-Zu-viele-E-Mails-So-schaltest-du-Benachrichtigungen-aus)

Der Radar ordnet Antworten in **Besichtigung**, **Rückfrage**, **Absage**, **Vorsicht** oder **Sonstiges** ein. Er schreibt kurz dazu, worum es geht, und setzt eine Priorität. Standardmäßig nutzt er lokale Regeln. Mit `KLEINANZEIGEN_JUDGE_PROVIDER=mistral` und deinem `MISTRAL_API_KEY` sendet er für die Einordnung einen kurzen Antwortausschnitt an Mistral. Er entfernt erkennbare E-Mail-Adressen, Telefonnummern und Links.

- `KLEINANZEIGEN_REPLY_MODE=draft` bereitet Antworten zur Durchsicht vor. Sie stehen unter `python src/munich_radar.py klein-reply-drafts`.
- `KLEINANZEIGEN_REPLY_MODE=auto` darf eine klare Besichtigungseinladung oder eine Frage nach weiterem Interesse einmal pro Inserat unverbindlich beantworten. Dafür braucht der Radar einen eindeutigen Nachrichtenverlauf und das angemeldete Kleinanzeigen-Profil. Termine, Unterlagen, Zahlungen und unklare Fälle bleiben bei dir. Die Option für Erstbewerbungen (`KLEINANZEIGEN_AUTOSEND`) lässt sich unabhängig davon setzen.

Mit `python src/munich_radar.py klein-activity` siehst du Treffer, Ortsprüfungen, Versandstatus und bewertete Antworten. Wenn du diese Hinweise automatisch per E-Mail bekommen willst, setze `RESEND_API_KEY`, `MUNICH_NOTIFY_EMAIL_FROM` (Absender aus deinem Resend-Konto) und `MUNICH_NOTIFY_EMAIL_TO` (deine E-Mail-Adresse). Der Versand läuft über die [Resend HTTPS API](https://resend.com/docs/api-reference/emails/send-email) und funktioniert auch auf Railway Hobby. Ältere Hinweise findest du bis zu 30 Tage im lokalen Verlauf.

### Auf Railway betreiben

Verbinde das Repo als [Dockerfile-Service](https://docs.railway.com/builds/dockerfiles) mit Railway. Wähle für den Dienst die [Region EU West (Amsterdam)](https://docs.railway.com/deployments/regions). Das Image startet den Worker und enthält Chromium und den zugehörigen Treiber. Betreibe eine Instanz und hänge ein privates [Volume](https://docs.railway.com/volumes) unter `/data` an. Dort bleiben Entwürfe, Versandstatus und Browserprofil erhalten. Eine öffentliche Domain brauchst du nur für das Webdashboard.

Trage die Variablen aus der lokalen Anleitung in Railway ein, inklusive `MUNICH_MESSAGE_TEMPLATE` als mehrzeilige Variable. Lass den Dienst zuerst im Entwurfsmodus laufen. Der Versand braucht einen erfolgreichen Browserstart mit aktiver Chromium-Sandbox und eine reguläre Anmeldung. Für Kleinanzeigen muss das Browserprofil auf dem Host bereits angemeldet sein. Ohne interaktive Anmeldung auf dem Host kannst du den Kleinanzeigen-Versand lokal betreiben.

Mit [Railway SSH](https://docs.railway.com/cli/ssh) kannst du Entwürfe und Versandstatus lesen:

```bash
railway ssh -- start-mietradar python /app/src/munich_radar.py drafts
railway ssh -- start-mietradar python /app/src/munich_radar.py delivery
railway ssh -- start-mietradar python /app/src/munich_radar.py klein-review
railway ssh -- start-mietradar python /app/src/munich_radar.py klein-activity
railway ssh -- start-mietradar python /app/src/munich_radar.py klein-reply-drafts
```

Die folgenden Checks verwenden die bereits hinterlegten Railway-Variablen. Der Mistral-Check prüft zwei fiktive Nachrichten, die nie verschickt werden. Der E-Mail-Check sendet ausdrücklich eine einzelne Testmail an die konfigurierte Empfängeradresse.

```bash
railway ssh -- python /app/src/judge_smoke.py
railway ssh -- python /app/src/integration_smoke.py kleinanzeigen
railway ssh -- python /app/src/integration_smoke.py email
```

Die Idee, neue Inserate für eine persönliche Nachricht zu priorisieren, kommt von [jonasdieker/wg-gesucht-bot](https://github.com/jonasdieker/wg-gesucht-bot). Der München-Radar setzt diesen Ablauf eigenständig um.

## What is MietRadar?

The original MietRadar bots monitor WG-Gesucht and ImmobilienScout24 and automatically apply to listings matching their search filters. They use the same application generator as the Munich radar and support local Gemma, Google Gemini, OpenAI, and Mistral Studio.

**Keywords**: Wohnung Bot, WG Bot, apartment bot Germany, Wohnungssuche automatisieren, WG-Gesucht Bot, ImmobilienScout24 Bot, rental application automation, Mietwohnung Bot, automated apartment search Germany, Bewerbung automatisch senden

**Disclaimer**: Use at your own risk. Respect the platforms' terms of service. This tool is provided "as is" without warranty.

## Features

| Feature | Description |
|---|---|
| 🏠 **Dual Platform** | Supports both WG-Gesucht and ImmobilienScout24 |
| 🔄 **Zero-Touch Loop** | Scans search results every N minutes, applies to new listings automatically |
| 🥷 **Stealth Automation** | Chrome with anti-detection patches, human-like typing & clicking |
| 🚫 **Smart Blacklisting** | Auto-blocks sent listings, skips sponsored/agency ads, manual override lists |
| 🧠 **AI Personalisation** | Multi-provider LLM (local Gemma 3 4B-IT, Google Gemini, or OpenAI) generates a unique paragraph per listing (optional) |
| 📊 **Reply Tracking** | CSV reports with 🟢/🟡/🔴 status for each application |
| ⚡ **Shared Pipeline** | Single message template, LLM persona, and helper functions for both platforms |

## Directory Structure
```text
miet-radar/
├── config/                         # User-editable configurations
│   ├── .env                        # Secrets (credentials, URLs, HF token)
│   ├── .env.example                # Template for .env
│   ├── llm_persona.txt             # LLM prompt/identity (editable)
│   ├── message.txt                 # Shared message template (both bots)
│   ├── message.txt.example         # Template for message.txt
│   ├── wg_blacklist.txt            # Manual blacklist for WG-Gesucht
│   ├── wg_blacklist.txt.example    # Template
│   ├── immo_blacklist.txt          # Manual blacklist for ImmoScout24
│   └── immo_blacklist.txt.example  # Template
├── data/                           # Runtime data (auto-generated, DO NOT EDIT)
│   ├── wg_diff.dat                 # Processed WG IDs
│   ├── wg_sent_request.dat         # WG audit log with timestamps
│   ├── wg_replies_report.csv       # WG reply tracker
│   ├── wg_offer.json               # WG scan snapshot
│   ├── wgbot_profile/              # WG browser session cache
│   ├── immo_diff.dat               # Processed ImmoScout IDs
│   ├── immo_sent_request.dat       # ImmoScout audit log
│   ├── immo_replies_report.csv     # ImmoScout reply tracker
│   ├── immo_offer.json             # ImmoScout scan snapshot
│   └── immobot_profile/            # ImmoScout browser session cache
├── src/                            # Core Python application logic
│   ├── wg-gesucht.py               # Main loop — WG-Gesucht
│   ├── submit_wg.py                # WG browser automation & form submission
│   ├── check_replies.py            # WG reply tracker
│   ├── immoscout.py                # Main loop — ImmobilienScout24
│   ├── submit_immo.py              # IS24 browser automation & form submission
│   ├── check_replies_immo.py       # IS24 reply tracker
│   └── llm_personalizer.py         # Shared LLM inference (Gemma 3 4B-IT, Gemini, OpenAI)
├── tests/                          # Tests (no live sends)
│   ├── test_llm_personalizer.py    # WG end-to-end test
│   ├── test_immo.py                # IS24 end-to-end test
│   ├── test_stealth.py             # Browser stealth validation
│   └── test_llm_only.py            # Isolated LLM test
├── scripts/
│   └── setup.sh                    # One-command setup script
├── README.md
└── requirements.txt
```

## Quick Start

### 1. Prerequisites
- Python 3.10+
- Google Chrome installed

### 2. Setup (One Command)
```bash
git clone https://github.com/unik-w/MietRadar
cd miet-radar
bash scripts/setup.sh
```
The setup script creates a virtual environment, installs all dependencies, and scaffolds config files from templates.

### 2b. Manual Install (Alternative)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env
cp config/message.txt.example config/message.txt
```

### 3. Configuration
1. **Credentials** — Edit `config/.env`:
   - **WG-Gesucht**: `WG_EMAIL`, `WG_PASSWORD`, `WG_SEARCH_URLS`
   - **ImmoScout24**: `IMMO_EMAIL`, `IMMO_PASSWORD`, `IMMO_SEARCH_URLS` and personal details (`IMMO_FIRST_NAME`, `IMMO_LAST_NAME`, etc.) for the IS24 contact form.
   - *(Optional)* `HF_TOKEN` for LLM Personalisation.

2. **Message Template** — Edit `config/message.txt`:
   - Use `{name}` for the landlord's name (auto-filled)
   - Use `{LLM_TEXT}` for the AI-generated paragraph (auto-filled when LLM is enabled)

3. **LLM Persona** *(Optional)* — Edit `config/llm_persona.txt`:
   Set `USE_LLM_PERSONALIZATION=true` in `.env` to enable. The persona auto-adapts to WG vs. apartment listings.

   Choose the LLM backend via `LLM_PROVIDER` in `.env`:
   | `LLM_PROVIDER` | Backend | Required `.env` vars |
   |---|---|---|
   | `gemma_local` *(default)* | Local Gemma 3 4B-IT (HuggingFace + PyTorch, runs on your machine) | `HF_TOKEN` |
   | `gemini` | Google Gemini API — Developer API key **or** Vertex AI | `GEMINI_MODEL` + either `GEMINI_API_KEY` **or** (`GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and `gcloud auth application-default login`) |
   | `openai` | OpenAI API (e.g. GPT-5 nano) | `OPENAI_MODEL`, `OPENAI_API_KEY` |
   | `mistral` | Mistral Studio Chat Completions | `MISTRAL_API_KEY`, optional `MISTRAL_MODEL` |

   Install the extra SDKs only for the backend(s) you use: `pip install google-genai` (Gemini) and/or `pip install openai` (OpenAI).

   **💸 Free credits tip:** new Google Cloud accounts get **$300 in free credit** (valid ~90 days),
   usable against Vertex AI usage — not just Gemini. Vertex AI's **Model Garden** also hosts
   third-party models (e.g. **Anthropic Claude**) and open-source models (Llama, Mistral, and
   others), so the same credit can be used to experiment with those too, not only Google's own
   models. To use Vertex AI instead of a plain Gemini API key:
   1. Install the gcloud CLI: `brew install --cask google-cloud-sdk` (macOS)
   2. `gcloud auth application-default login` (one-time browser login)
   3. `gcloud services enable aiplatform.googleapis.com --project=<your-project-id>`
   4. Set `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION` in `.env`

   Note: model availability varies by project/region on Vertex AI — if a model 404s, check the
   [Model Garden](https://console.cloud.google.com/vertex-ai/model-garden) for exact IDs enabled
   for your project, or try `location=global` for newer preview models.

4. **Blacklists**:
   - `config/wg_blacklist.txt` — paste WG-Gesucht URLs to ignore
   - `config/immo_blacklist.txt` — paste ImmoScout expose IDs to ignore

### 4. First Run & CAPTCHAs 🧩
On first run, the platform will likely show a **CAPTCHA**. The bot detects this and **pauses** — solve it manually in the browser window. Subsequent runs use saved session cookies from `data/wgbot_profile/` or `data/immobot_profile/`.

## Usage

### Run the WG-Gesucht Bot
```bash
source venv/bin/activate
python src/wg-gesucht.py
```

### Run the ImmobilienScout24 Bot
```bash
source venv/bin/activate
python src/immoscout.py
```

Both bots run independently as continuous loops. They scan multiple search result pages, apply to new listings, update blacklists, and sleep for `CHECK_INTERVAL_SECONDS` (configurable in `.env`).

## Reply Tracking 📊

Generate a report of all sent applications and their reply status:

| Command | Report File |
|---|---|
| `python src/check_replies.py` | `data/wg_replies_report.csv` |
| `python src/check_replies_immo.py` | `data/immo_replies_report.csv` |

**Status indicators:**
- 🟢 **Replied** — host has responded
- 🟡 **Pending** — no reply yet, sent < 3 days ago
- 🔴 **No Reply** — no response after 3+ days

## Testing (Dry Run)

Test the full pipeline **without sending** any messages:

```bash
# WG-Gesucht — scrape + LLM personalisation
python tests/test_llm_personalizer.py --listings 3

# ImmobilienScout24 — scrape + LLM personalisation
python tests/test_immo.py --listings 3

# ImmoScout24 — template only, no LLM (fast scraping test)
python tests/test_immo.py --no-llm --listings 3

# Browser stealth validation
python tests/test_stealth.py
```

---

## How It Works

```
┌─────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────────┐
│  Scan search │───▶│ Filter: skip │───▶│ Extract desc  │───▶│ Generate msg │
│  result pages│    │ sponsored &  │    │ + contact name│    │ (template +  │
│  (paginated) │    │ blacklisted  │    │ from expose   │    │  LLM paragraph│
└─────────────┘    └──────────────┘    └───────────────┘    └──────┬───────┘
                                                                   │
                                                                   ▼
                                                           ┌──────────────┐
                                                           │ Fill form &  │
                                                           │ send message │
                                                           │ (stealth)    │
                                                           └──────────────┘
```

---

## Credits & History 📜
This project is an advanced evolution of the original [immo](https://github.com/nickirk/immo) repository by [nickirk](https://github.com/nickirk). 

While **MietRadar** has been completely restructured and modernized with Python 3 support, dual-platform automation, Selenium stealth techniques, and LLM-based personalization, the original git history has been merged and preserved to ensure proper attribution to the foundational work of the original contributors.
