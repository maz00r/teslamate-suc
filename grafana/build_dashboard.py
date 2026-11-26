"""Builds grafana/teslamate-suc.json. The queries live here, in one place. Run: python3 grafana/build_dashboard.py"""

import json
import os

DS = {"type": "grafana-postgresql-datasource", "uid": "${ds}"}
CAR = "car_id = ANY(ARRAY[$car_id]::int[]) AND provider = ANY(ARRAY[$provider]::text[])"
WHERE = f"WHERE $__timeFilter(start_date) AND {CAR}"
PROV_LABEL = "CASE provider WHEN 'tesla' THEN 'Tesla' WHEN 'greenway' THEN 'Greenway' ELSE 'ręczny' END"

QUERIES = {
    "spend": f"SELECT sum(invoice_cost) AS \"Wydatki\" FROM suc.v_charges {WHERE}",
    "grid": f"SELECT sum(grid_kwh) AS \"Energia z sieci\" FROM suc.v_charges {WHERE}",
    "price": f"SELECT sum(charging_fee) FILTER (WHERE full_payment) / nullif(sum(grid_kwh) FILTER (WHERE full_payment), 0) AS \"Śr. cena\" "
             f"FROM suc.v_charges {WHERE}",
    "loss": f"SELECT 100 * (sum(grid_kwh) - sum(charge_energy_added)) / nullif(sum(grid_kwh), 0) AS \"Straty\" "
            f"FROM suc.v_charges {WHERE}",
    "loss_kwh": f"SELECT sum(loss_kwh) AS \"Straty kWh\" FROM suc.v_charges {WHERE}",
    "idle": f"SELECT coalesce(sum(idle_fee), 0) AS \"Opłaty postojowe\" FROM suc.v_charges {WHERE}",
    "sessions": f"SELECT count(*) AS \"Ładowania\", count(*) FILTER (WHERE free) AS \"Darmowe\" FROM suc.v_charges {WHERE}",
    "monthly": f"""SELECT date_trunc('month', start_date) AS time,
       sum(invoice_cost) AS "Wydatki [zł]",
       sum(grid_kwh) AS "Energia z sieci [kWh]"
  FROM suc.v_charges {WHERE}
 GROUP BY 1 ORDER BY 1""",
    "price_ts": f"""SELECT start_date AS time, price_per_kwh AS "Cena [zł/kWh]"
  FROM suc.v_charges {WHERE} AND price_per_kwh IS NOT NULL ORDER BY 1""",
    "loss_ts": f"""SELECT start_date AS time, loss_pct AS "Straty [%]", loss_kwh AS "Straty [kWh]"
  FROM suc.v_charges {WHERE} ORDER BY 1""",
    "sites": f"""SELECT {PROV_LABEL} AS "Źródło", coalesce(site_name, '(ręcznie)') AS "Ładowarka",
       count(*) AS "Ładowania",
       sum(grid_kwh) AS "kWh z sieci",
       sum(invoice_cost) AS "Wydatki",
       sum(charging_fee) FILTER (WHERE full_payment) / nullif(sum(grid_kwh) FILTER (WHERE full_payment), 0) AS "Śr. cena",
       100 * (sum(grid_kwh) - sum(charge_energy_added)) / nullif(sum(grid_kwh), 0) AS "Straty %",
       coalesce(sum(idle_fee), 0) AS "Postój"
  FROM suc.v_charges {WHERE}
 GROUP BY 1, 2 ORDER BY "Wydatki" DESC""",
    "list": f"""SELECT start_date AS "Start",
       {PROV_LABEL} AS "Źródło",
       coalesce(site_name, '(ręcznie)') AS "Ładowarka",
       charge_energy_added AS "kWh do baterii",
       grid_kwh AS "kWh z sieci",
       loss_pct AS "Straty %",
       price_per_kwh AS "Cena",
       charging_fee AS "Ładowanie",
       idle_fee AS "Postój",
       invoice_cost AS "Razem",
       CASE WHEN free THEN 'darmowe' WHEN NOT paid THEN 'nieopłacone' ELSE '' END AS "Status",
       match_kind AS "Dopasowanie",
       charging_process_id AS "id",
       car_id
  FROM suc.v_charges {WHERE}
 ORDER BY start_date DESC""",
    "unmatched": f"""SELECT start_utc AS "Start", {PROV_LABEL} AS "Źródło", site_name AS "Ładowarka",
       grid_kwh AS "kWh z sieci", total AS "Kwota", currency AS "Waluta", status AS "Status", session_id AS "Sesja"
  FROM suc.invoice
 WHERE status IN ('unmatched', 'review') AND $__timeFilter(start_utc) AND provider = ANY(ARRAY[$provider]::text[])
 ORDER BY start_utc DESC""",
}

PLN = "currencyPLN"


def stat(pid, title, q, unit, x, w, decimals=None, color="blue"):
    o = {"id": pid, "type": "stat", "title": title, "datasource": DS,
         "gridPos": {"h": 4, "w": w, "x": x, "y": 0},
         "targets": [{"refId": "A", "datasource": DS, "rawSql": QUERIES[q], "format": "table", "editorMode": "code"}],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": "value", "graphMode": "none", "textMode": "value_and_name" if q == "sessions" else "auto"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color}}, "overrides": []}}
    if decimals is not None:
        o["fieldConfig"]["defaults"]["decimals"] = decimals
    return o


def panel(pid, kind, title, q, grid, **extra):
    o = {"id": pid, "type": kind, "title": title, "datasource": DS, "gridPos": grid,
         "targets": [{"refId": "A", "datasource": DS, "rawSql": QUERIES[q],
                      "format": "time_series" if kind in ("timeseries", "barchart_ts") else "table",
                      "editorMode": "code"}],
         "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}
    o.update(extra)
    return o


panels = [
    stat(1, "Wydatki na ładowanie publiczne", "spend", PLN, 0, 4, 2, "orange"),
    stat(2, "Energia z sieci", "grid", "kwatth", 4, 4, 0),
    stat(3, "Średnia cena", "price", "suffix: zł/kWh", 8, 4, 2, "purple"),
    stat(4, "Straty ładowania", "loss", "percent", 12, 3, 1, "red"),
    stat(5, "Straty", "loss_kwh", "kwatth", 15, 3, 0, "red"),
    stat(6, "Opłaty postojowe", "idle", PLN, 18, 3, 2, "yellow"),
    stat(7, "Ładowania", "sessions", "none", 21, 3, 0, "green"),
]

monthly = panel(10, "timeseries", "Wydatki i energia w miesiącu", "monthly", {"h": 9, "w": 12, "x": 0, "y": 4})
monthly["fieldConfig"] = {"defaults": {"custom": {"drawStyle": "bars", "fillOpacity": 70, "barAlignment": 0,
                                                  "lineWidth": 1}},
                          "overrides": [
                              {"matcher": {"id": "byName", "options": "Wydatki [zł]"},
                               "properties": [{"id": "unit", "value": PLN}, {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]},
                              {"matcher": {"id": "byName", "options": "Energia z sieci [kWh]"},
                               "properties": [{"id": "unit", "value": "kwatth"}, {"id": "custom.axisPlacement", "value": "right"},
                                              {"id": "custom.drawStyle", "value": "line"}, {"id": "custom.fillOpacity", "value": 0},
                                              {"id": "custom.showPoints", "value": "always"},
                                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]}]}
monthly["interval"] = "1M"
monthly["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}

price = panel(11, "timeseries", "Cena za kWh (każde ładowanie)", "price_ts", {"h": 9, "w": 12, "x": 12, "y": 4})
price["fieldConfig"] = {"defaults": {"unit": "suffix: zł/kWh", "decimals": 2,
                                     "custom": {"drawStyle": "points", "pointSize": 7, "showPoints": "always"},
                                     "color": {"mode": "fixed", "fixedColor": "purple"}}, "overrides": []}

loss = panel(12, "timeseries", "Straty ładowania (kWh z faktury − kWh do baterii)", "loss_ts",
             {"h": 9, "w": 12, "x": 0, "y": 13})
loss["fieldConfig"] = {"defaults": {"custom": {"drawStyle": "points", "pointSize": 7, "showPoints": "always"}},
                       "overrides": [
                           {"matcher": {"id": "byName", "options": "Straty [%]"},
                            "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                                           {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
                           {"matcher": {"id": "byName", "options": "Straty [kWh]"},
                            "properties": [{"id": "unit", "value": "kwatth"}, {"id": "decimals", "value": 1},
                                           {"id": "custom.axisPlacement", "value": "right"},
                                           {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}]}

money_cols = ["Wydatki", "Postój"]
sites = panel(13, "table", "Ładowarki", "sites", {"h": 9, "w": 12, "x": 12, "y": 13})
sites["fieldConfig"] = {"defaults": {"decimals": 1}, "overrides": [
    *[{"matcher": {"id": "byName", "options": c}, "properties": [{"id": "unit", "value": PLN}, {"id": "decimals", "value": 2}]}
      for c in money_cols],
    {"matcher": {"id": "byName", "options": "Śr. cena"}, "properties": [{"id": "unit", "value": "suffix: zł/kWh"}, {"id": "decimals", "value": 2}]},
    {"matcher": {"id": "byName", "options": "Straty %"}, "properties": [{"id": "unit", "value": "percent"}]},
    {"matcher": {"id": "byName", "options": "kWh z sieci"}, "properties": [{"id": "unit", "value": "kwatth"}, {"id": "decimals", "value": 0}]},
    {"matcher": {"id": "byName", "options": "Ładowania"}, "properties": [{"id": "decimals", "value": 0}]},
]}
sites["options"] = {"showHeader": True, "sortBy": [{"displayName": "Wydatki", "desc": True}]}

lst = panel(14, "table", "Ładowania z fakturą", "list", {"h": 12, "w": 24, "x": 0, "y": 22})
lst["fieldConfig"] = {"defaults": {"decimals": 2}, "overrides": [
    *[{"matcher": {"id": "byName", "options": c}, "properties": [{"id": "unit", "value": PLN}]}
      for c in ("Ładowanie", "Postój", "Razem")],
    {"matcher": {"id": "byName", "options": "Cena"}, "properties": [{"id": "unit", "value": "suffix: zł/kWh"}]},
    {"matcher": {"id": "byName", "options": "Straty %"}, "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}]},
    {"matcher": {"id": "byName", "options": "Start"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}]},
    {"matcher": {"id": "byName", "options": "id"}, "properties": [{"id": "decimals", "value": 0}, {"id": "links", "value": [
        {"title": "Szczegóły ładowania (TeslaMate)",
         "url": "/d/BHhxFeZRz/charge-details?var-car_id=${__data.fields.car_id}&var-charging_process_id=${__value.raw}"}]}]},
    {"matcher": {"id": "byName", "options": "car_id"}, "properties": [{"id": "custom.hidden", "value": True}]},
]}
lst["options"] = {"showHeader": True}

un = panel(15, "table", "Sesje bez ładowania w TeslaMate / do przeglądu", "unmatched", {"h": 6, "w": 24, "x": 0, "y": 34})
un["fieldConfig"] = {"defaults": {"decimals": 2}, "overrides": [
    {"matcher": {"id": "byName", "options": "Start"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}]}]}

panels += [monthly, price, loss, sites, lst, un]

dashboard = {
    "uid": "teslamate-suc",
    "title": "Ładowanie publiczne — faktury",
    "tags": ["TeslaMate", "SuC", "Greenway"],
    "timezone": "browser",
    "schemaVersion": 39,
    "editable": True,
    "time": {"from": "now-1y", "to": "now"},
    "description": "Faktury z Superchargerów i Greenway oraz koszty ręczne (dodatek TeslaMate SuC): wydatki, cena za kWh, straty ładowania, opłaty postojowe.",
    "links": [{"title": "TeslaMate", "type": "dashboards", "tags": ["tesla"], "asDropdown": True}],
    "templating": {"list": [
        {"name": "ds", "label": "Baza", "type": "datasource", "query": "grafana-postgresql-datasource",
         "current": {"text": "TeslaMate", "value": "TeslaMate"}, "hide": 2},
        {"name": "car_id", "label": "Auto", "type": "query", "datasource": DS,
         "query": "SELECT id AS __value, coalesce(name, 'Auto ' || id) AS __text FROM cars ORDER BY display_priority, id",
         "refresh": 1, "includeAll": True, "multi": True, "allValue": None,
         "current": {"text": "All", "value": "$__all"}},
        {"name": "provider", "label": "Źródło", "type": "custom", "includeAll": True, "multi": True,
         "query": "Tesla : tesla, Greenway : greenway, Ręczne : manual",
         "options": [{"text": "All", "value": "$__all", "selected": True},
                     {"text": "Tesla", "value": "tesla", "selected": False},
                     {"text": "Greenway", "value": "greenway", "selected": False},
                     {"text": "Ręczne", "value": "manual", "selected": False}],
         "current": {"text": "All", "value": "$__all"}},
    ]},
    "panels": panels,
}

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "teslamate-suc.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(dashboard, f, ensure_ascii=False, indent=2)
    print(out)
