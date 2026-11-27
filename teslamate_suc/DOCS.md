# TeslaMate SuC — faktury Supercharger dla TeslaMate

Dodatek Home Assistant, który pobiera historię ładowań i faktury z Superchargerów
(Tesla Fleet API) i uzupełnia koszty ładowań w TeslaMate. Liczy też straty ładowania,
cenę za kWh i opłaty postojowe.

Koszt trafia do `charging_processes.cost` tylko wtedy, gdy jest pusty albo wcześniej wpisał go ten dodatek.
Szczegóły (kWh z sieci, straty, cena/kWh, opłata postojowa) są w schemacie `suc`, a widok `suc.v_charges` jest dla Grafany.
Domyślnie działa `dry_run: true`: panel pokazuje, co zostałoby zapisane, a TeslaMate zostaje bez zmian.

## Przygotowanie (jednorazowo)

1. **Cloudflare Worker** dla `suc.<twoja-domena>`: ustaw domenę w `cloudflare/wrangler.toml`, potem
   `cd cloudflare && npx wrangler@4.14.0 login && npx wrangler@4.14.0 deploy`. Wrangler sam doda
   Custom Domain z certyfikatem. Sprawdź:
   `https://suc.<domena>/.well-known/appspecific/com.tesla.3p.public-key.pem` zwraca klucz,
   a `https://suc.<domena>/callback` wyświetla stronę.
2. **developer.tesla.com** → Twoja aplikacja:
   - Allowed Origin: `https://suc.<domena>`
   - Allowed Redirect URI: `https://suc.<domena>/callback`
   - Scopes: `vehicle_charging_cmds` (wystarczy; `vehicle_device_data` opcjonalnie)
   - Billing: podpięta karta i niski limit.
3. **Home Assistant** → Dodatki → Sklep → ⋮ → Repozytoria → `https://github.com/maz00r/teslamate-suc`.
   Zainstaluj **TeslaMate SuC** i w konfiguracji ustaw `client_id`, `client_secret`, `region: eu`,
   `domain: suc.<domena>`, `redirect_uri: https://suc.<domena>/callback`. Uruchom.
4. **Panel SuC** (menu boczne HA):
   1. „Zarejestruj domenę” — jednorazowa rejestracja partnera w regionie EU.
   2. „Zaloguj do Tesli” → zaloguj się → skopiuj adres ze strony `/callback` → wklej → „Zakończ logowanie”.
   3. „Uruchom sondę” (np. 365 dni) → „Kopiuj podsumowanie” i wklej je do rozmowy.
      Podsumowanie zawiera strukturę pól i typy opłat. Surowa odpowiedź leży w `/data/probe.json`.

5. **Baza:** w konfiguracji ustaw `db_host` (np. `db21ed7f-postgres-latest`), `db_user`, `db_password`, `db_name: teslamate`
   (te same dane co TeslaMate). W panelu kliknij „Podgląd (dry-run)” z zakresem dat obejmującym całą historię,
   przejrzyj plan, zrób backup HA, wyłącz `dry_run` i kliknij „Zapisz koszty”.

**Greenway:** w panelu SuC przeciągnij link „Kopiuj token Greenway” na pasek zakładek. Zaloguj się na
client.greenwaypolska.pl, kliknij zakładkę i wklej token w panelu. Potem podgląd/zapis ze źródłem „Greenway”, a dla
automatu włącz `greenway_enabled`. Token wygasa po ~60 dniach, HA przypomni o wymianie.

**Koszt ręczny:** sekcja „Koszt ręczny” w panelu: wyszukaj ładowanie, wpisz kwotę (i ewentualnie kWh z licznika).

**Niezamknięte ładowania:** sekcja w panelu pokazuje ładowania bez zapisanego końca i zamyka je tak jak TeslaMate (bez kosztu). Cofanie w „Zapisy i cofanie”.

**Grafana:** zaimportuj `grafana/teslamate-suc.json` (Dashboards → New → Import) do folderu TeslaMate.
Pokazuje wydatki, cenę za kWh, straty ładowania, opłaty postojowe, Superchargery i faktury bez dopasowania.
Zapytania są w `grafana/build_dashboard.py`.

Cofnięty zapis wyklucza ładowanie z synchronizacji. Żeby je przywrócić:
`DELETE FROM suc.cost_excluded WHERE charging_process_id = <id>;`

## Bezpieczeństwo

- Panel jest dostępny tylko przez Ingress HA. Inne adresy dostają 403.
- Token Tesli leży w `/data/token.json` (0600). Refresh token jest jednorazowy, więc nowy
  zapisujemy atomowo, zanim zostanie użyty. Dodatek rotuje go co tydzień, żeby nie wygasł
  (po 3 mies. nieużywania).
- Zakresy: `openid offline_access vehicle_charging_cmds`. Bez `vehicle_cmds`.
- Prywatny klucz EC nie jest potrzebny do historii ładowań i nie trafia do repo ani do dodatku.
