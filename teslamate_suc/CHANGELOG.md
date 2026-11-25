# Zmiany

## 0.2.4
- `zero_is_empty` (domyślnie włączone): cudzy koszt 0,00 przy płatnej fakturze traktujemy jak pusty.
  TeslaMate wpisuje 0 dla geofence'a ze stawką 0 (np. „Tesla Ząbki”). Inne niezerowe koszty pozostają chronione.

## 0.2.3
- Plan pokazuje przy pominiętych ładowaniach geofence z TeslaMate (nazwa i stawka) oraz `free_supercharging`, czyli skąd wziął się cudzy koszt.

## 0.2.2 — poprawki po niezależnym przeglądzie
- **Krawędź okna:** zapis tylko dla grup, które dotykają właściwego zakresu. Grupa ucięta przez margines
  (np. sesja podzielona o północy) nie nadpisuje już poprawnego kosztu połową kwoty.
- Nasz koszt, który stracił dopasowanie, jest raportowany (`stale`, sensor `suc_stale`), a nie zostawiany po cichu.
- Cofnięcie jest trwałe: proces trafia do `suc.cost_excluded`. Cofnięcie korekty przywraca „własność” poprzedniej wartości.
- Cofanie jest zablokowane przy `dry_run`.
- `overwrite_cost` wyłącza kontrolę współbieżności tylko dla świadomego nadpisania cudzego kosztu.
- Ujemne kwoty: poprawny podział i odrzucanie ujemnego `cost`.
- Zmiana czasów, VIN-u lub lokalizacji faktury też aktualizuje `suc.invoice`. Data „do” w panelu obejmuje cały dzień.

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
