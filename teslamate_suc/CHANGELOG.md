# Zmiany

## 0.2.1
- Obraz: dołożona `libpq`, bez niej psycopg się nie importował (0.2.0 nie startowała).

## 0.2.0
- Synchronizacja co `sync_interval_hours` (domyślnie 6 h) za ostatnie `lookback_days` dni, a w panelu backfill z zakresem dat.
- Parser potwierdzony na 54 prawdziwych sesjach: czasy z offsetem → UTC, koszt = suma `totalDue` (brutto),
  CREDIT → koszt 0,00, CONGESTION (postój) liczona osobno, UNPAID oznaczane.
- Dopasowanie do `charging_processes` (tylko DC) przez przecięcie przedziałów: 1:1, N:1 (Tesla dzieli sesję o północy
  przy zmianie taryfy), 1:N (podział wg kWh), M:N rozstrzygane po najdłuższym nakładaniu, reszta „do przeglądu”.
- Własny schemat `suc` (bez FK do TeslaMate) i widok `suc.v_charges` ze stratami, ceną/kWh i opłatą postojową.
- Zapis kosztu tylko, gdy jest pusty albo pochodzi od nas (korekty faktur). `overwrite_cost` nadpisuje cudze.
  Undo log i cofanie całego zapisu z panelu.
- `dry_run: true` domyślnie: podgląd planu w panelu, zero zmian w TeslaMate.
- Sensory w HA: `sensor.suc_*` i powiadomienie, gdy Tesla odrzuci token.

## 0.1.0
- Etap 1: logowanie do Tesli (authorization_code + wklejany adres z /callback), rotacja refresh tokena,
  rejestracja domeny partnera, sonda `charging/history` z podsumowaniem struktury, licznik płatnych żądań.
