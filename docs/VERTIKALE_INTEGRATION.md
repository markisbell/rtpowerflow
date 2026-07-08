# Vertikale Integration MS/NS — Umsetzungsplan

> Status: **Phasen 0, 1, 2, 3, 4, 6 und 5/netzsim umgesetzt** (Branch
> `feature/vertical`, Stand 2026-07-08); offen: nur noch die
> gridedit-Editorseite von Phase 5 (Station-Panel-Auswahl + Export/Reload
> von `lv_ref` + E-Check-Warnung). netzsim konsumiert `lv_ref` bereits
> vollständig: `gridedit_mv_import` löst die Referenz relativ zur MV-Datei
> auf (user_grids), splict das gridformat-NS-Netz durch seinen eigenen, auf
> die MS-Spannung des Zielnetzes umgeschnappten Stationstrafo, macht die
> Gebäudelasten zu LPG-Haushalten und die Station zur echten Zelle;
> fehlende Referenz → Summenlast + Warnhinweis in den Notes. Phase 4 komplett: Seitenpanel-Sektion **„Zellen"**
> (scrollbare Tabelle aller ONS mit Ampel-Punkt [grün ruhig · gelb dimmt
> auf Signal · rot Station überlastet · grau kein Messwert], Stations-
> Messwert, 📟/🎛-Icons; Klick zoomt die Karte per `focusBuses`/fitBounds
> in die Zelle und heftet ihre Station an, „← Bezirkssicht" zoomt zurück).
> Designentscheidung: Die Ampel lebt in der Tabelle; auf der Karte
> markiert der rote Ring die dimmenden Stationen (ein Ampel-FILL der
> Marker würde mit der Spannungs-Färbung kollidieren). Der rONT (ront.py) stuft ±4 × 1,5 % auf der
> HS-Seite, hält die Sammelschiene im Totband um den Sollwert, sieht nur
> Messung/Schätzung (Telegramm-Taktung wie die Regler, blind hält) und
> synchronisiert seine Stufenstellung als Betreiber-Sollwert in die
> Schätzmodelle (Estimator/Zellstufen kopieren die tap-Spalten je Lauf). Die Phase-4-Scheibe liefert den Klick-Lehrpfad für
> Szenario 4: Steuerbox per Element-Menü an jeder Zellstation (gesplicet:
> Sammelschiene/Trafo, Summenlast: MS-Bus), Netzampel-Koordinator per Klick
> auf den UW-Bus, Seitenpanel-Sektion „Netzampel" (Koordinator-Status,
> EV/PV-Signal, Zellen/Boxen/dimmen-Statistik) und rote Signal-Ringe an
> dimmenden Stationen auf der Karte. Szenario 4 liegt unter `data/scenarios/4-feierabend-…`,
> der Picker (`data/grid_library.json`) trägt dafür den Bezirk
> `mv_rural_3150` + seine zwei weiteren NS-Netze (E5). Wesentliche
> Erkenntnis aus der Kalibrierung: Die ding0-MS-Ringe sind geschlossen
> und ein MS-Segment ist nur über VIELE Stationen überlastbar — die
> Feierabend-Welle sind daher aggregierte 200-kW-Wallbox-Blöcke an den 42
> Summenlast-Stationen des Rings L19/L222 (8,4 MW), mit Steuerboxen an
> ebendiesen Zellen (`cell`-Scope gilt jetzt auch für Summenlast-Zellen).
> Zweite Erkenntnis: schätzungsgespeiste Regler müssen je NEUEM Telegramm
> handeln (`Controller.est_stamp`/`est["seq"]`), sonst schwingt der Kreis
> auf Bezirken hart (Sensor langsamer als Aktor). Abweichung in Phase 2 gegenüber dem
> Plan: Der Koordinator sendet EIN einheitliches Signal an alle Zellenregler
> (gleicher Faktor = proportionale Lastreduktion je Zelle, E3 erfüllt);
> außerdem führt ein lokal messloser Zellenregler empfangene Signale AUS —
> der Befehlsweg braucht eine Steuerbox, kein Messgerät. Unkoordinierbar
> sind Zellen ohne Regler. Befund aus 1.2: Auf dem Bezirk
> `mv_rural_3150` ist die hierarchische Schätzung **gleich genau** wie die
> monolithische (max |ΔU| ≈ 3 mpu bei digitalen ONS), aber **nicht schneller**
> (≈ 1,16 s vs. ≈ 1,06 s je Lauf) — nur 3 der 157 Zellen sind straßengeroutet,
> der reduzierte MS-Graph behält 247 Busse. Der Tempovorteil kommt erst mit
> mehr gesplicten Zellen; der Wert der Hierarchie liegt hier in der Didaktik
> (Randfluss-Quellen meter/estimate/pseudo, Fehler je Zelle, VNB-Schichtung).
> Ziel: netzsim vom „einen Netz je Ebene" zum **vertikal integrierten
> Smart Grid** erweitern — Mittelspannung (MS) und Niederspannung (NS)
> physikalisch, messtechnisch und regelungstechnisch durchgängig, mit
> Drill-down in der Oberfläche und einem eigenen Referenzszenario.

---

## 1. Motivation und Zielbild

Heute simuliert netzsim wahlweise ein NS-Ortsnetz, ein MS-Netz mit
Summenlasten oder (über den District-Import) bereits ein **zusammengesetztes
MS+NS-Gebiet** als einen einzigen Lastfluss. Was fehlt, ist die
**Smart-Grid-Vertikale**: Die Ebenen kennen einander nicht als Struktur —
es gibt keine Ortsnetz-Zellen als Objekte, keine hierarchische
Zustandsschätzung (NS-Zellen liefern dem MS-Betreiber aggregierte
Ersatzwerte) und keine kaskadierte Regelung (ein MS-Engpass kann heute
nicht koordiniert auf die §14a-Regler der betroffenen Ortsnetze wirken).

Das didaktische Zielbild spiegelt die reale VNB-Praxis:

1. **Physik:** ein Netz, ein Lastfluss — 110-kV-Slack, HS/MS-Trafo,
   MS-Ring, Ortsnetzstationen (ONS), straßengeroutete NS-Zellen. *(existiert)*
2. **Beobachtbarkeit:** je Ebene eigene Messwelten — SCADA/RLM auf MS,
   digitale ONS an der Trafogrenze, SMGWs in der NS-Zelle. Die Schätzung
   arbeitet **hierarchisch**: Zellen schätzen lokal, ihre Randflüsse werden
   Ersatzmesswerte der MS-Schätzung. *(neu)*
3. **Regelung:** Netzampel-Kaskade — ein MS-Koordinator sieht (nur über
   Messung/Schätzung!) den MS-Engpass und signalisiert den Zellenreglern
   eine Reduktion; die Zellen dimmen lokal nach §14a-Logik. *(neu)*
4. **Bedienung:** MS-Karte mit Zellen-Kennzahlen und Ampelfarbe je Station,
   Klick auf eine ONS öffnet die Zelle. *(neu)*
5. **Lehrpfad:** Referenzszenario 4 — „Der Engpass, den keine Zelle sieht":
   jede Zelle einzeln unter der Grenze, der MS-Strang darüber. *(neu)*

---

## 2. Ist-Stand (worauf der Plan aufsetzt)

| Baustein | Datei(en) | Stand |
|---|---|---|
| District-Composer: MS-Ring + gesplicete OSM-NS-Netze über den echten Stationstrafo, Rest bleibt Summenlast; Elemente tragen Namenspräfix `lv{grid_id}:` | `src/netzsim/district_import.py` | fertig, getestet (`tests/test_district_import.py`) |
| Katalog-Dispatch: MS-Manifest-Einträge subsumieren die NS-Netze ihres Bezirks (`lv_subgrids`) und bauen das Verbundnetz | `src/netzsim/grid_catalog.py` (Z. 71 ff., 174 f.) | fertig |
| LPG/EV/PV nur auf echte Haushalte (`household`-Flag), Summenlasten behalten synthetische Profile | `district_import.py`, `loadgen/assign.py` | fertig |
| Ebenen-Umschalter MS/NS/Alle auf der Live-Karte | `ui/src/components/MapDiagram.tsx` | fertig |
| WLS-Schätzung **monolithisch** über das ganze Verbundnetz (475-Knoten-Bezirk: ~1–1,6 s je Lauf, Raster-Stufen 15/30/60/120 min) | `src/netzsim/estimator.py`, `simulator.py` | fertig, skaliert aber schlecht |
| Überlast-Regler: Scope `station` = **das ganze Netz**, `bus` = ein Knoten; gespeist nur aus Messung + Schätzung | `src/netzsim/controller.py`, `simulator._controller_update` | fertig — `station` ist auf Bezirken **semantisch falsch** (drosselt alle Zellen pauschal) |
| Volles Manifest mit 6 MS-Bezirken + 14 NS-Netzen (Picker aktuell auf die 2 Referenz-NS-Netze getrimmt) | `data/grid_library_full.json` / `data/grid_library.json` | Daten vorhanden |
| gridedit: eigene MS-Netze (Format `gridedit-mv`), Stationen als **Summenlasten** | `gridedit`, `src/netzsim/gridedit_mv_import.py` | fertig — keine eigenen NS-Zellen anschließbar |

**Die drei Lücken:** (a) keine Zellen als erstklassiges Laufzeit-Konzept,
(b) keine hierarchische Beobachtbarkeit/Schätzung, (c) keine vertikale
Regelkaskade. Dazu UI-Drill-down, gridedit-Anschluss und Lehrszenario.

---

## 3. Architektur-Zielbild

```
                        110 kV (Slack)
                            │  HS/MS-Trafo  ◄── MS-Koordinator (scope "mv")
                 ┌──────────┴──────────┐        sieht: SCADA + MS-Schätzung
                 │      MS-Ring        │        wirkt: Zellen-Signal (Ampel)
            ONS A│                ONS B│...
       ┌─────────┴───┐        ┌────────┴────┐
       │ Zelle A     │        │ Zelle B     │   je Zelle:
       │ (NS-Netz)   │        │ (Summenlast │   - digitale ONS (Trafo-Messung)
       │ SMGWs, §14a-│        │  = degene-  │   - lokale WLS-Schätzung
       │ Zellenregler│        │  rierte     │   - Zellenregler (scope "cell")
       └─────────────┘        │  Zelle)     │   - Randfluss → Ersatzmesswert MS
                              └─────────────┘
```

Datenfluss der hierarchischen Schätzung je Schritt (im Messraster):
NS-Zelle: SMGW-Messwerte + Pseudolasten → **Zellen-WLS** → geschätzter
Randfluss an der ONS → als virtuelle Messung in die **MS-WLS** auf dem
reduzierten MS-Graphen (Zellen zu Injektionen kollabiert). Regelkaskade:
MS-Koordinator liest NUR MS-Messung/-Schätzung → Ampel je Zelle →
Zellenregler drosseln lokal EV/PV (Richtung wie heute: Export → PV,
Import → EV) → nächster Schritt.

---

## 4. Phasenplan

Jede Phase ist eine eigene Runde im Projektstil: Backend → Tests →
Live-Nachweis → ggf. UI → Commit. Aufwand in „Runden" (halbe bis ganze
Arbeitssitzung).

### Phase 0 — Zellen als erstklassiges Konzept (Fundament, 1 Runde)

Ohne Zellenobjekt bleibt alles Weitere String-Gefrickel auf Namenspräfixen.

- **`grid_inputs.py`**: neues Feld `cells: list[CellSpec]` auf `GridInputs` —
  `{cell_id, name, station_trafo (Index in transformers), lv_busbar (Bus-Index),
  mv_bus (Bus-Index), buses: [..], lumped: bool}`. `district_import.convert_district`
  füllt es beim Splicen (die Information liegt dort bereits vor, Z. 51–63);
  Summenlast-Stationen werden **degenerierte Zellen** (`lumped: true`,
  `buses: []`). Reine NS-/MS-Netze: genau eine bzw. null Zellen.
- **`network_builder.py` / `simulator.py`**: Zellen in den Simulator tragen;
  abgeleitete Indizes einmalig aufbauen (`cell_of_bus`, `cell_of_line`,
  Zeilenmengen `ev_rows`/`pv_rows` je Zelle — analog `_loads_at`).
- **API**: `/network` liefert `cells[]` (id, name, Busmengen, Trafo, lumped);
  `topology()` erweitert.
- **Tests**: District-Kompositionstest prüft Zellenzahl/-zuordnung; NS-Netz
  hat 1 Zelle; Zellsummen (Busmengen disjunkt + vollständig).
- **Nachweis**: Bezirk laden, `/network.cells` gegen Manifest zählen.

### Phase 1 — Hierarchische Beobachtbarkeit & Schätzung (2–3 Runden)

- **Runde 1.1 — Mess-Presets je Zelle** (`measurements.py`, API, UI):
  Presets `digital_stations` (Trafo-Messung an jeder ONS), `cell_full:{id}`
  (SMGW-Vollausbau einer Zelle). `placement()`/Abdeckung um Zellen-Statistik
  ergänzen (`coverage.cells: {id: {nodes, trafo}}`).
- **Runde 1.2 — Zwei-stufige WLS** (`estimator.py`):
  `HierarchicalEstimator` orchestriert: (1) je Zelle mit ≥ 1 Messgerät eine
  **lokale WLS** auf dem herausgetrennten Zellnetz (Slack = LV-Sammelschiene,
  Sollwert aus MS-Schätzung des Vorschritts, Start: 1,0 pu); (2) Randfluss
  (P/Q durch den Stationstrafo) je Zelle — gemessen (digitale ONS) vor
  geschätzt vor Pseudo (Summen-SLP der Zelle, breite σ); (3) **MS-WLS** auf
  dem reduzierten Graphen (Zellen → äquivalente Lasten am MS-Bus).
  Konfiguration über die bestehende Schätz-Richtlinie:
  `EstConfig.hierarchy: "auto" | "monolithic" | "hierarchical"`
  (`auto` = hierarchisch, sobald `cells` existieren). `StepResult.estimated`
  erhält `mode` + je Zelle `cell_error` (nur im Nicht-Strikt-Modus).
- **Runde 1.3 — Tages-Sweep & Performance**: `daily_est` nutzt denselben
  Pfad; Kostenmessung auf dem 475-Knoten-Bezirk. Erwartung: Zellen-WLS
  (30–60 Knoten) kostet Millisekunden, MS-WLS auf ~100–150 Knoten wenige
  100 ms → **hierarchisch schlägt monolithisch (~1,6 s) deutlich** und
  erlaubt feinere Raster auf Bezirken. Cache-Signatur um `hierarchy`
  erweitern.
- **Tests**: Zellen-WLS exakt bei Vollausbau; Randfluss-Aggregat vs. Wahrheit;
  Honesty-Tripwire auf Bezirksebene (ungemessene Zelle → MS-Schätzung darf
  deren internes Detail NICHT kennen, nur den Pseudo-Randfluss);
  monolithisch vs. hierarchisch Fehlervergleich.
- **Nachweis**: Bezirk, 2 Zellen gemessen, Rest dunkel → Schätzfehler je
  Zelle + MS-Ebene in der Übersicht.

### Phase 2 — Vertikale Regelkaskade / Netzampel (2 Runden)

- **Runde 2.1 — Scope `cell`** (`controller.py`, `simulator.py`):
  Zellenregler = heutige `station`-Semantik, aber auf die Zellen-Zeilenmengen
  aus Phase 0 begrenzt (Domäne: eigener Stationstrafo + Zell-Leitungen aus
  der Schätzung; Hebelrichtung je Zellen-Randfluss). Auf reinen NS-Netzen
  bleibt `station` ein Alias auf die eine Zelle (**Rückwärtskompatibilität**:
  bestehende Szenarien laden unverändert).
- **Runde 2.2 — MS-Koordinator** (`scope: "mv"`): Domäne = MS-Leitungen +
  HS/MS-Trafo, gespeist ausschließlich aus Messung/MS-Schätzung. Er drosselt
  **nicht selbst**, sondern setzt je Zelle ein Ampelsignal
  `cell_signal: {cell_id: faktor}` (grün 1,0 / gelb / rot), das die
  Zellenregler als zusätzliche Obergrenze auf ihre Faktoren nehmen
  (`min(lokal, signal)`). Verteilungspolitik: **proportional zur
  Zellen-Randlast** (Entscheidung E3, s. u.). Zellen ohne Regler oder ohne
  Messdaten bleiben unkoordinierbar → der Engpass verschwindet nur teilweise
  (Kernlehre: Regelgüte = Beobachtbarkeit, jetzt vertikal).
- **API/UI**: `POST /controller {scope: "cell"|"mv", cell?}`;
  `StepResult.controllers` um `cell`/`signals` erweitert; Szenarien
  persistieren wie gehabt; UI: Regler-Badge an der ONS, Koordinator-Block
  mit Signaltabelle.
- **Tests**: Kaskade regelt einen MS-Strang-Engpass ab (voll gemessen,
  `sim._est_wall = 0.0`-Muster aus `test_controller.py`); blinde Zelle wird
  nicht koordiniert; NS-Alias-Kompatibilität.
- **Nachweis**: live am Bezirk (s. Phase 6, Szenario 4).

### Phase 3 — rONT: regelbarer Ortsnetztrafo (optional, 1–2 Runden)

Der Klassiker der vertikalen Spannungsintegration: Stufensteller je ONS
(`net.trafo.tap_pos`, pandapower-nativ), Regelziel Spannung an der
LV-Sammelschiene (Sollband z. B. 1,00 ± 0,015 pu), gespeist aus der
**Zellen-Messung/Schätzung** (konsequent: ohne Spannungsmesswert in der
Zelle regelt der rONT blind auf die Stationsmessung). Platzierbar wie
Batterie/Regler, Szenario-persistiert, Sweep-fähig (Stufenverlauf im
Tagesgraphen). Entkoppelt NS-Spannung vom MS-Profil — sichtbar im
Spannungs-Tagesgraphen einer fernen Zelle. *Kann nach Phase 2 oder parallel
zu Phase 4 laufen; Entscheidung E4.*

### Phase 4 — UI: Drill-down & Zellen-Aggregate (2 Runden)

- **Runde 4.1 — MS-Sicht mit Zellenwissen**: Stationsmarker tragen
  Ampelfarbe (aus Koordinator-Signal bzw. Zellen-Auslastung der aktiven
  Sicht) + Tooltip mit Zellen-KPIs (gemessene/geschätzte Randlast, Abdeckung,
  Reglerstatus). Neue Seitenpanel-Sektion **„Zellen"**: Tabelle aller ONS
  (Ampel · Randfluss · Abdeckung · Regler), Klick heftet die Zelle an.
- **Runde 4.2 — Drill-down**: Klick auf eine ONS → „Zelle öffnen" fokussiert
  Karte/Schema auf die Zellbusse (Erweiterung des vorhandenen
  MS/NS/Alle-Umschalters um einen Zellenfilter `focusCell`); Breadcrumb
  „Bezirk ▸ Zelle A" zum Zurückspringen. Alle drei Sichten
  (Lastfluss/Gemessen/Schätzung) respektieren den Fokus.
- **Tests**: vitest für die Zellen-Sektion; Live-Nachweis mit Screenshots.

### Phase 5 — gridedit: eigene NS-Zellen am eigenen MS-Netz (2 Runden, beide Repos)

- **gridedit**: eine MS-Station kann statt „Summenlast" eine Referenz auf
  ein gezeichnetes NS-Netz tragen (`lv_ref`: Dateiname eines
  gridformat-Exports + Trafo-Typ). Export `gridedit-mv` v2 schreibt die
  Referenzliste; der Editor prüft im E-Check, dass die referenzierte Datei
  existiert und ihre Sammelschienen-Spannung passt.
- **netzsim** (`gridedit_mv_import.py`): löst `lv_ref` gegen
  `data/user_grids/` auf und splict wie `district_import` (gleicher
  Code-Pfad → Phase-0-Zellen inklusive); fehlende Referenz → Warnhinweis in
  `notes`, Station bleibt Summenlast.
- Damit ist der volle Kreis geschlossen: **selbst gezeichnetes vertikales
  Smart Grid** von 110 kV bis zum Hausanschluss.

### Phase 6 — Referenzszenario 4 + Handbuch (1–2 Runden)

- **Szenario 4 — „Feierabend im Bezirk: der Engpass, den keine Zelle sieht"**:
  MS-Bezirk (Kandidat: der Bezirk der beiden Referenz-NS-Netze; das getrimmte
  `grid_library.json` wird um genau diesen einen MS-Eintrag ergänzt),
  EV-Welle 17–19 Uhr über alle Zellen. Kalibrierungsziel: jede Zelle einzeln
  ≤ ~85 %, aber HS/MS-Trafo bzw. MS-Strang > 105 %. Messkonzept: digitale
  ONS an allen Stationen (keine SMGWs nötig — die Pointe lebt von den
  Randflüssen). Ablauf im Unterricht: (1) Zellensicht — alles grün;
  (2) MS-Sicht mit hierarchischer Schätzung — Strang rot; (3) MS-Koordinator
  platzieren → faire Zellen-Drosselung, Engpass verschwindet, Zellen dimmen
  nur wenige Prozent. Spiegelbild von Szenario 3, eine Ebene höher.
- **Benutzerhandbuch**: neues Kapitel „Vertikale Integration" (Konzept,
  Zellen-UI, Kaskade, Szenario-4-Walkthrough mit verifizierten Zahlen,
  Screenshots per Tour-Skript); CLAUDE.md-Abschnitt analog.

---

## 5. Querschnittsthemen

- **Performance-Budget**: Bezirks-Wahrheits-Sweep (recycle) ist heute schon
  tragbar; Ziel der Hierarchie ist, die **Schätzung** auf Bezirken vom
  120-min- in Richtung 15-min-Raster zu holen. Messen in Phase 1.3, Raster-
  Pinning-Logik (`_est_sweep_min`) bleibt unverändert zuständig.
- **Recorder/Export**: neue Spalten schleifen automatisch durch (der
  Recorder schreibt das Wire-Format); zusätzlich `cells.csv` (je Schritt und
  Zelle: Randfluss gemessen/geschätzt, Ampel, Reglerfaktoren) für
  Übungsauswertungen.
- **Rückwärtskompatibilität**: reine NS-Netze verhalten sich exakt wie heute
  (eine implizite Zelle; `station`-Regler unverändert); gespeicherte
  Szenarien und Aufzeichnungen bleiben gültig; `gridedit-mv` v1-Dateien
  laden weiter (Stationen als Summenlast).
- **Strikter Modus**: Zellen-Fehlermetriken (`cell_error`) zählen zur
  Wahrheit und werden wie `estimated.error` gestrippt.
- **Kein Locking**: alle neuen Laufzeit-Mutationen (Koordinator, rONT)
  folgen der Projektkonvention „selbstheilend statt Locks".

## 6. Offene Entscheidungen (mit Empfehlung)

| # | Frage | Empfehlung |
|---|---|---|
| E1 | Hierarchische Schätzung als Default auf Bezirken? | Ja (`hierarchy: "auto"`); monolithisch bleibt als Vergleichs-/Lehrmodus wählbar |
| E2 | Zellen-WLS-Slack-Sollwert: fest 1,0 pu oder MS-Schätzwert des Vorschritts? | MS-Schätzwert mit 1,0-pu-Fallback — genauer und didaktisch ehrlicher |
| E3 | Fairness der Koordinator-Drosselung | proportional zur gemessenen/geschätzten Zellen-Randlast; „rotierend" später als Option |
| E4 | rONT im Erstausbau? | Ja, aber nach Phase 2 (eigene Runde) — hoher Lehrwert, sauber abgrenzbar |
| E5 | Szenario-4-Bezirk in den getrimmten Picker? | Ja, genau ein MS-Eintrag zusätzlich (Kundenwunsch „kleiner Picker" bleibt gewahrt) |

## 7. Reihenfolge, Abhängigkeiten, Aufwand

```
Phase 0 (1 R) ──► Phase 1 (2–3 R) ──► Phase 2 (2 R) ──► Phase 6 (1–2 R)
     │                                     │
     ├──────────► Phase 4 (2 R) ◄──────────┘   (4.1 braucht nur 0; 4.2-Ampel braucht 2)
     └──────────► Phase 5 (2 R)                (unabhängig von 1–2)
Phase 3 (1–2 R) nach Phase 2, parallel zu 4/5 möglich
```

Gesamt: **11–14 Runden**. Minimaler didaktischer Durchstich („Szenario 4
zeigbar"): Phasen 0 → 1.1/1.2 → 2 → 6 ≈ **7 Runden**.
