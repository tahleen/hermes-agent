"""Memory status rollup for ``/api/status``.

Read side for signals the gateway already persists: the 30s ``state/gateway.heartbeat``
(RSS + MemAvailable/MemTotal + swap) and the lifecycle sentinel's ``suspected_oom``
flag — two small file reads, no IPC.  ``/api/status`` is unauthenticated, so only
coarse numbers (MB), enums and booleans.  A missing/corrupt file degrades to
``pressure="unknown"`` rather than raising into the status endpoint.

The read side also reads the HOST sample written by the house cron
(``state/host-mem.json``, 5-min cadence) because the VM thresholds below are
fractions of the VM's own cap and cannot see the host.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Thresholds on system MemAvailable.  ``critical`` doubles as the lifecycle
# ledger's OOM-suspicion heuristic: a level that makes a later unclean death
# "suspected OOM" already warns while the process is alive.
_CRITICAL_AVAILABLE_KIB = 64 * 1024  # < 64 MiB available
_CRITICAL_AVAILABLE_FRACTION = 0.05  # < 5% of MemTotal
_ELEVATED_AVAILABLE_KIB = 128 * 1024  # < 128 MiB available
_ELEVATED_AVAILABLE_FRACTION = 0.15  # < 15% of MemTotal
_PRESSURE_TIERS = (  # order-sensitive: worst first
    ("critical", _CRITICAL_AVAILABLE_KIB, _CRITICAL_AVAILABLE_FRACTION),
    ("elevated", _ELEVATED_AVAILABLE_KIB, _ELEVATED_AVAILABLE_FRACTION),
)

# Writer cadence is 30s; 150s tolerates a briefly stalled loop without letting
# a long-dead gateway's last sample pose as current.
_HEARTBEAT_FRESH_TTL_S = 150.0

# ---- host arm -------------------------------------------------------------
# The thresholds above are read INSIDE the VM, so they are a fraction of the VM's own cap
# (``memory=`` in .wslconfig) and cannot see the resource that actually runs out when the VM
# grows: the host's.  Measured 03/10/2026 in the same second -- the VM read "11.6 GiB of 15.6
# free" (ok) while the host sat at 63.7% used, with the VM still able to claim ~7 GiB more.
# Both host numbers are ABSOLUTE and DERIVED from the machine geometry by the producer
# (~/.hermes/scripts/veille-host-mem.py), which publishes that derivation in the state file's
# ``seuils`` block; the two constants below are only the DEFAULTS for when that block is absent
# or malformed -- the live guard-rail follows ``seuils["libre_go"]`` / ``seuils["compression_go"]``
# so it cannot keep a second, silently drifting derivation.  Recalibrated 04/10/2026:
#   free floor          = host total - non-VM footprint - VM cap = 31.9 - 11.44 - 16.0 = 4.46
#                         -> posed at 5.0 Go, so it fires before the worst case is reached
#   compression ceiling = 4.5 Go, posed above the highest value ever observed (4.19 Go over the
#                         68 samples 03/10 21:52 -> 04/10 09:59); the old 2.0 Go spoke about the
#                         NORMAL regime and is now only a fallback
# Re-derive both when ``memory=`` or the DIMMs change.  The lifecycle ledger keeps its VM-only
# verdict: its evidence is the heartbeat's VM sample, the host arm widens the LIVE tier only.
_HOST_MEM_FREE_FLOOR_GB = 5.0
_HOST_COMPRESSION_CEILING_GB = 2.0
# The producer writes every 5 min (cron `ea1204309698`, `4-59/5`); a heartbeat-style 150 s TTL
# would abandon the file for half of every window, so the host arm goes quiet after two missed
# ticks plus a margin.
_HOST_STATE_WRITER_CADENCE_S = 300.0
_HOST_STATE_FRESH_TTL_S = 2 * _HOST_STATE_WRITER_CADENCE_S + 100.0
_HOST_STATE_RELATIVE = ("state", "host-mem.json")


def _nonneg_int(value: Any) -> Optional[int]:
    """Return *value* if it is a non-negative int (bools rejected), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _mb(kib: Any) -> Optional[int]:
    return None if _nonneg_int(kib) is None else kib // 1024


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed is not None and parsed.tzinfo is None else parsed


def classify_pressure(available_kib: Any, total_kib: Any) -> str:
    """``ok``/``elevated``/``critical`` from MemAvailable/MemTotal; ``unknown`` when the
    sample is missing/malformed — "could not read it" must never read as "fine"."""
    available, total = _nonneg_int(available_kib), _nonneg_int(total_kib)
    if available is None:
        return "unknown"
    fraction = available / total if total else None
    for level, kib_floor, frac_floor in _PRESSURE_TIERS:
        if available < kib_floor or (fraction is not None and fraction < frac_floor):
            return level
    return "ok"


def get_host_mem_state_path(home: Optional[Path] = None) -> Path:
    """``<HERMES_HOME>/state/host-mem.json`` — the host sample (see the module docstring)."""
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return home.joinpath(*_HOST_STATE_RELATIVE)


def _nonneg_number(value: Any) -> Optional[float]:
    """Return *value* if it is a finite non-negative int/float (bools rejected), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    # Chained comparison rejects NaN (False on the first leg) and infinities alike, so a
    # malformed value cannot pose as a real reading.
    return number if 0 <= number < float("inf") else None


def _host_threshold(state: Any, key: str, default: float) -> float:
    """The producer's derived threshold *key* from ``state["seuils"]``, else *default*.

    The producer publishes the derivation of its own thresholds; reading them here keeps the
    guard-rail from carrying a second, silently drifting derivation.  A missing/mistyped or
    malformed value (bool, str, None, <= 0, NaN/inf) falls back to the module default.
    """
    seuils = state.get("seuils") if isinstance(state, dict) else None
    value = _nonneg_number(seuils.get(key)) if isinstance(seuils, dict) else None
    return default if value is None or value <= 0 else value


def _is_fresh(sampled_at: Optional[datetime], moment: datetime, ttl_s: float) -> bool:
    return sampled_at is not None and 0 <= (moment - sampled_at).total_seconds() <= ttl_s


def _host_compression_confirmed(state: Any) -> bool:
    """True once the producer's consecutive-sample counter backs the compression reading.

    Compression is noisy at rest (~1 Go baseline, 3-4 Go spikes measured 03/10/2026): one
    sample over the ceiling is not a verdict, N consecutive ones are.  ``alerte_en_cours`` is
    the flag the operator's Telegram alert rides (``streak >= n`` plus hysteresis), so the
    guard-rail and the message agree.  That ``alerte_en_cours``/``streak`` counter rides THREE
    motifs (free memory, compression, page file) because the producer exposes no per-motif
    counter, so the compression arm is confirmed by any sustained host alert.
    """
    if not isinstance(state, dict):
        return False
    if state.get("alerte_en_cours") is True:
        return True
    seuils = state.get("seuils")
    n = seuils.get("n") if isinstance(seuils, dict) else None
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        n = 3
    streak = state.get("streak")
    return isinstance(streak, int) and not isinstance(streak, bool) and streak >= n


def classify_host_pressure(state: Any) -> str:
    """``ok``/``critical``/``unknown`` for the HOST arena, from the host state file.

    ``unknown`` = the host arm has nothing to say (no usable sample) and the caller keeps its
    VM verdict.  ``critical`` = the host is out of pocket for the VM: free memory below the
    ABSOLUTE floor (acute — every byte the VM claims now comes out of Windows), or compression
    held above the ABSOLUTE ceiling over consecutive samples.
    """
    sample = state.get("dernier_echantillon") if isinstance(state, dict) else None
    if not isinstance(sample, dict):
        return "unknown"
    free = _nonneg_number(sample.get("mem_free_gb"))
    compression = _nonneg_number(sample.get("compress_gb"))
    if free is None and compression is None:
        return "unknown"
    free_floor = _host_threshold(state, "libre_go", _HOST_MEM_FREE_FLOOR_GB)
    compression_ceiling = _host_threshold(state, "compression_go", _HOST_COMPRESSION_CEILING_GB)
    if free is not None and free < free_floor:
        return "critical"
    if (
        compression is not None
        and compression > compression_ceiling
        and _host_compression_confirmed(state)
    ):
        return "critical"
    return "ok"


def combine_pressure(*levels: str) -> str:
    """Worst of several arena verdicts; ``unknown`` only when nothing else spoke."""
    for level in ("critical", "elevated", "ok"):
        if level in levels:
            return level
    return "unknown"


def _read_host_state(home: Optional[Path]) -> Optional[Dict[str, Any]]:
    """The host sample, ``None`` when unreadable (never raises)."""
    try:
        from gateway.lifecycle_ledger import _read_json

        return _read_json(get_host_mem_state_path(home))
    except Exception:
        return None


def host_pressure(home: Optional[Path] = None, *, now: Optional[datetime] = None) -> str:
    """Freshness-gated host verdict, for callers that already hold their own VM sample.

    An absent or stale ``sampled_at`` makes the host arm abstain (``unknown``): the VM rule is
    then the only one left — never a default "ok", never a phantom "critical".
    """
    state = _read_host_state(home)
    if not state:
        return "unknown"
    sample = state.get("dernier_echantillon")
    if not isinstance(sample, dict):
        return "unknown"
    sampled_at = _parse_iso(sample.get("sampled_at"))
    moment = now or datetime.now(timezone.utc)
    if not _is_fresh(sampled_at, moment, _HOST_STATE_FRESH_TTL_S):
        return "unknown"
    return classify_host_pressure(state)


def _read_state_files(home: Optional[Path]) -> tuple:
    """``(heartbeat, sentinel)`` dicts, each ``None`` when unreadable."""
    try:
        from gateway.lifecycle_ledger import _read_json, get_lifecycle_sentinel_path
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        return _read_json(get_loop_heartbeat_path(home)), _read_json(get_lifecycle_sentinel_path(home))
    except Exception:
        return None, None


def collect_memory_status(
    home: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """``memory`` block for ``/api/status``; ``home`` scopes to a profile (``None`` =
    active), ``now`` is injectable.  Never raises — a down gateway or corrupt files
    yield ``pressure="unknown"`` plus whatever fields could be recovered."""
    moment = now or datetime.now(timezone.utc)
    status: Dict[str, Any] = {
        "pressure": "unknown", "gateway_rss_mb": None, "system_total_mb": None, "system_available_mb": None,
        "swap_used_mb": None, "host_available_mb": None,
        "host_compression_mb": None, "host_sampled_at": None,
        "sampled_at": None, "last_boot_unclean": False, "last_boot_suspected_oom": False,
        # Identity of the CURRENT life (sentinel started_at): the dashboard keys
        # banner dismissal on it so acknowledging one OOM restart does not mute the NEXT.
        "boot_id": None,
    }

    heartbeat, sentinel = _read_state_files(home)
    if heartbeat:
        sampled_at, mem = _parse_iso(heartbeat.get("updated_at")), heartbeat.get("mem")
        if isinstance(mem, dict):
            for dst, src in (("gateway_rss_mb", "rss_kib"), ("system_total_mb", "mem_total_kib"),
                             ("system_available_mb", "mem_available_kib"), ("swap_used_mb", "swap_used_kib")):
                status[dst] = _mb(mem.get(src))
            if sampled_at is not None:
                status["sampled_at"] = sampled_at.isoformat()
                # Stale sample: numbers still reported (sampled_at says when) but
                # pressure stays "unknown" so a dead gateway's final gasp cannot
                # render a live "critical" banner forever.
                if _is_fresh(sampled_at, moment, _HEARTBEAT_FRESH_TTL_S):
                    status["pressure"] = classify_pressure(mem.get("mem_available_kib"), mem.get("mem_total_kib"))

    host_state = _read_host_state(home)
    sample = host_state.get("dernier_echantillon") if host_state else None
    host_level = "unknown"
    if isinstance(sample, dict):
        # Numbers are reported even when the sample is stale (``host_sampled_at`` says when);
        # only the verdict needs a fresh one.
        free = _nonneg_number(sample.get("mem_free_gb"))
        compression = _nonneg_number(sample.get("compress_gb"))
        status["host_available_mb"] = None if free is None else int(free * 1024)
        status["host_compression_mb"] = None if compression is None else int(compression * 1024)
        host_sampled_at = _parse_iso(sample.get("sampled_at"))
        if host_sampled_at is not None:
            status["host_sampled_at"] = host_sampled_at.isoformat()
            if _is_fresh(host_sampled_at, moment, _HOST_STATE_FRESH_TTL_S):
                host_level = classify_host_pressure(host_state)
    status["pressure"] = combine_pressure(status["pressure"], host_level)

    if sentinel:
        status["last_boot_unclean"] = bool(sentinel.get("prior_unclean_exit"))
        status["last_boot_suspected_oom"] = bool(sentinel.get("prior_suspected_oom"))
        started_at = sentinel.get("started_at")
        status["boot_id"] = started_at if isinstance(started_at, str) and started_at else None

    return status


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import logging  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'logger': ('gateway.run', 'logger'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
