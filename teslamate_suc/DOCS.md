# TeslaMate SuC — faktury Supercharger dla TeslaMate

Dodatek Home Assistant, który pobiera historię ładowań i faktury z Superchargerów
(Tesla Fleet API) i uzupełnia koszty ładowań w TeslaMate. Liczy też straty ładowania,
cenę za kWh i opłaty postojowe.

**Etap 1 (wersja 0.1.x):** logowanie do Tesli, rejestracja domeny partnera i sonda
historii ładowań. Do bazy TeslaMate nie trafia jeszcze nic.

## Przygotowanie (jednorazowo)

1. **Cloudflare Worker** dla `suc.<twoja-domena>`: wklej `cloudflare/worker.js` jako nowy Worker
   (Workers & Pages → Create → Hello World → Edit code). Potem w Settings → Domains & Routes
   dodaj Custom Domain `suc.<twoja-domena>`. Sprawdź:
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

## Bezpieczeństwo

- Panel jest dostępny tylko przez Ingress HA. Inne adresy dostają 403.
- Token Tesli leży w `/data/token.json` (0600). Refresh token jest jednorazowy, więc nowy
  zapisujemy atomowo, zanim zostanie użyty. Dodatek rotuje go co tydzień, żeby nie wygasł
  (po 3 mies. nieużywania).
- Zakresy: `openid offline_access vehicle_charging_cmds`. Bez `vehicle_cmds`.
- Prywatny klucz EC nie jest potrzebny do historii ładowań i nie trafia do repo ani do dodatku.
