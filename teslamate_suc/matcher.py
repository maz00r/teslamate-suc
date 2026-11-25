"""Matching Tesla invoices to TeslaMate charging_processes. Pure functions, no I/O.

Model: a bipartite graph. An edge means the same VIN and overlapping time intervals,
with a tolerance on each side. Connected component → group:
  1:1   one invoice, one process                → the whole invoice goes to the process
  N:1   Tesla split the session (the probe shows it splits on a tariff change at midnight
        and when you unplug and replug within a minute), TeslaMate kept one process → sum
  1:N   TeslaMate split the session (lost connection, sleep) → split by energy_added
  M:N   if every invoice has a clear "best" process (the longest overlap WITHOUT tolerance)
        and every process gets something → resolved like N:1 per process;
        otherwise `review`: we write nothing, it shows in the panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from parse import CENT, Invoice

MAX_COST = Decimal("9999.99")  # charging_processes.cost is numeric(6,2)
REASON_OVERWRITE = "nadpisanie (overwrite_cost)"


@dataclass(frozen=True)
class Process:
    id: int
    car_id: int
    vin: str
    start: datetime
    end: datetime
    energy_added: Decimal | None
    cost: Decimal | None
    brand: str | None = None


@dataclass
class Group:
    invoices: list[Invoice]
    processes: list[Process]
    kind: str  # 1:1 | N:1 | 1:N | M:N | review


@dataclass(frozen=True)
class Allocation:
    session_id: str
    process_id: int
    energy_kwh: Decimal
    cost: Decimal
    charging_fee: Decimal
    congestion_fee: Decimal


# --------------------------------------------------------------------------- graph


def _overlap_s(a0, a1, b0, b1) -> float:
    return (min(a1, b1) - max(a0, b0)).total_seconds()


def touches(inv: Invoice, p: Process, tol: timedelta) -> bool:
    return inv.vin == p.vin and _overlap_s(inv.start - tol, inv.end + tol, p.start, p.end) > 0


def build_groups(invoices: list[Invoice], processes: list[Process], tolerance_min: float = 10):
    """Returns (groups, unmatched_invoices, lonely_processes)."""
    tol = timedelta(minutes=tolerance_min)
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_vin: dict[str, list[Process]] = {}
    for p in processes:
        by_vin.setdefault(p.vin, []).append(p)
        find(("p", p.id))
    for inv in invoices:
        find(("i", inv.session_id))
        for p in by_vin.get(inv.vin, []):
            if touches(inv, p, tol):
                union(("i", inv.session_id), ("p", p.id))

    comps: dict = {}
    inv_by = {i.session_id: i for i in invoices}
    proc_by = {p.id: p for p in processes}
    for node in list(parent):
        comps.setdefault(find(node), []).append(node)

    groups, unmatched, lonely = [], [], []
    for nodes in comps.values():
        invs = sorted((inv_by[k] for t, k in nodes if t == "i"), key=lambda i: i.start)
        procs = sorted((proc_by[k] for t, k in nodes if t == "p"), key=lambda p: p.start)
        if invs and not procs:
            unmatched.extend(invs)
        elif procs and not invs:
            lonely.extend(procs)
        else:
            groups.append(_classify(invs, procs))
    groups.sort(key=lambda g: g.invoices[0].start)
    unmatched.sort(key=lambda i: i.start)
    lonely.sort(key=lambda p: p.start)
    return groups, unmatched, lonely


def _classify(invs: list[Invoice], procs: list[Process]) -> Group:
    ni, np_ = len(invs), len(procs)
    if np_ == 1:
        return Group(invs, procs, "1:1" if ni == 1 else "N:1")
    if ni == 1:
        return Group(invs, procs, "1:N")
    return Group(invs, procs, "M:N" if _best_assignment(invs, procs) else "review")


def _best_assignment(invs: list[Invoice], procs: list[Process]) -> dict[str, int] | None:
    """Each invoice → the process with the longest overlap without tolerance. None when that is
    ambiguous (no overlap, a tie) or some process ends up with no invoice."""
    out = {}
    for inv in invs:
        scored = sorted(((_overlap_s(inv.start, inv.end, p.start, p.end), p.id) for p in procs), reverse=True)
        best, second = scored[0], (scored[1] if len(scored) > 1 else (float("-inf"), None))
        if best[0] <= 0 or best[0] == second[0]:
            return None
        out[inv.session_id] = best[1]
    if set(out.values()) != {p.id for p in procs}:
        return None
    return out


# --------------------------------------------------------------------------- split


def split_exact(amount: Decimal, weights: list[Decimal], unit: Decimal = CENT) -> list[Decimal]:
    """Splits `amount` in proportion to the weights so the parts add up EXACTLY to `amount`
    (largest-remainder method). Zero weights → equal split."""
    n = len(weights)
    if n == 1:
        return [amount]
    if amount < 0:  # ROUND_DOWN truncates towards zero: compute on |amount| and restore the sign
        return [-x for x in split_exact(-amount, weights, unit)]
    total_w = sum(weights)
    if total_w <= 0:
        weights, total_w = [Decimal(1)] * n, Decimal(n)
    raw = [amount * w / total_w for w in weights]
    floors = [r.quantize(unit, rounding=ROUND_DOWN) for r in raw]
    rest = int(((amount - sum(floors)) / unit).to_integral_value())
    order = sorted(range(n), key=lambda k: (raw[k] - floors[k], -k), reverse=True)
    for k in order[:rest]:
        floors[k] += unit
    return floors


def allocate(g: Group) -> list[Allocation]:
    if g.kind == "review":
        return []
    if g.kind == "1:N":
        inv = g.invoices[0]
        w = [p.energy_added if p.energy_added and p.energy_added > 0 else Decimal(0) for p in g.processes]
        if sum(w) <= 0:
            w = [Decimal(str(max((p.end - p.start).total_seconds(), 0))) for p in g.processes]
        costs = split_exact(inv.total, w)
        chg = split_exact(inv.charging_fee, w)
        cng = split_exact(inv.congestion_fee, w)
        kwh = split_exact(inv.grid_kwh, w, Decimal("0.0001"))
        return [Allocation(inv.session_id, p.id, kwh[k], costs[k], chg[k], cng[k])
                for k, p in enumerate(g.processes)]
    if g.kind == "M:N":
        target = _best_assignment(g.invoices, g.processes)
    else:  # 1:1, N:1
        target = {i.session_id: g.processes[0].id for i in g.invoices}
    return [Allocation(i.session_id, target[i.session_id], i.grid_kwh, i.total, i.charging_fee, i.congestion_fee)
            for i in g.invoices]


def costs_by_process(allocs: list[Allocation]) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    for a in allocs:
        out[a.process_id] = out.get(a.process_id, Decimal(0)) + a.cost
    return out


# --------------------------------------------------------------------------- write decision


def decide(target: Decimal, current: Decimal | None, ours: Decimal | None, overwrite: bool) -> tuple[str, str]:
    """Returns (action, reason). action ∈ write | none | skip.
    `ours` = what we wrote earlier (proof the cost belongs to us)."""
    if target > MAX_COST or target < 0:
        return "skip", "kwota poza zakresem (ujemna albo > 9999.99)"
    if current is not None and current == target:
        return "none", "bez zmian"
    if current is None:
        return "write", "koszt pusty"
    if ours is not None and current == ours:
        return "write", "korekta faktury"
    if overwrite:
        return "write", REASON_OVERWRITE
    return "skip", "koszt ustawiony przez kogoś innego"
