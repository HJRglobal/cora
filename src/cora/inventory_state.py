"""Cross-channel F3E inventory state -- merge layer (A5 Part 2).

ONE CANONICAL MACHINE STORE, MANY HONEST VIEWS. Counts come from wherever the
stock physically sits; every figure carries its source and its `as_of`; and
absent is never rendered as zero (D-117 applied to units).

The store is a set of Drive JSON files, ONE PER WRITER, which kills clobber races
by construction (the D-102 single-writer pattern):

    f3e-inventory-shopify.json   <- Cora script (run_inventory_state_sync.py)
    f3e-inventory-channels.json  <- Cowork Chrome sweep (TikTok / Amazon / Walmart)
    f3e-inventory-manual.json    <- Cowork digest, transcribed from Airtable

WRITER DISCIPLINE. Only the first file is written by code that can guarantee
temp+rename semantics. The other two are written by Cowork tasks whose Write tool
cannot, so THIS LAYER DEFENDS ITSELF rather than assuming well-formed input:

  * a JSON parse failure renders that source UNKNOWN(parse-error) and falls back
    to a `.last-good` sibling this module maintains -- never a crash, and never
    silent absence, which would read as "we hold none";
  * every externally-authored string (SKU labels, location names, manual-count
    notes) is scrubbed before it can reach a Slack surface (D-118);
  * a non-numeric count renders UNPARSEABLE, never coerced to a number.

LABEL DISCIPLINE. Sales-CHANNEL names (Amazon FBA, Walmart WFS, TikTok FBT) are
operationally necessary and allowed. Data-SOURCE and tool names (Shopify, Seller
Central/Center, Polar) are not -- consistent with the ecom brief's convention and
f3e.md's source-opacity intent. Rendering helpers here emit channel names only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

SKU_MAP_PATH = _REPO_ROOT / "data" / "maps" / "f3e-channel-sku-map.yaml"

#: Store location, relative to the Founder-OS root.
STORE_RELDIR = Path("02-F3-Energy") / "inventory-state"

#: One file per writer. Keys are the source ids used throughout this module.
STORE_FILES: dict[str, str] = {
    "shopify": "f3e-inventory-shopify.json",
    "channels": "f3e-inventory-channels.json",
    "manual": "f3e-inventory-manual.json",
}

#: Channel keys carried by each source, in render order.
SHOPIFY_CHANNELS = ("office", "dtc_3pl", "unis", "tiktok_fbt")
SWEEP_CHANNELS = ("tiktok_fbt", "amazon_fba", "walmart_wfs")

#: Shopify location id -> channel key. Verified live 2026-08-04.
SHOPIFY_LOCATIONS: dict[str, int] = {
    "office": 81567023424,      # 1337 S Gilbert Rd -- manually managed
    "dtc_3pl": 110064533824,    # Nimbl -- real-time 3PL sync, the canonical DTC number
    "unis": 98823012672,        # UNIS (Cotton) -- fed by a WEEKLY upstream batch
    "tiktok_fbt": 111242608960, # marketplace-managed MIRROR; can drift from source
}

#: Per-channel provenance caveats, rendered so a number is never read as more
#: authoritative than it is.
CHANNEL_CAVEATS: dict[str, str] = {
    "unis": "weekly-fed",
    "tiktok_fbt": "mirror",
}

#: A Seller-Center block and the mirror disagreeing by at least this many units
#: is shown with BOTH figures rather than silently preferring one.
MIRROR_DISAGREEMENT_UNITS = 1

_CONTROL_RE = re.compile(r"[<>|`*_~\[\]]")
_WS_RE = re.compile(r"\s+")

# Stripping Slack control syntax is NOT sufficient here, and assuming otherwise
# was a real defect. The tool that renders this text is a VERBATIM_TABLE_TOOL, so
# `format_reply` is bypassed and only the egress boundary runs downstream -- and
# egress redacts bare URLs only for an allowlist of hosts (docs.google.com,
# drive.google.com, app.asana.com, notion.so, *.intuit.com). An arbitrary URL
# typed into the Airtable "Manual Counts" location field therefore reached an F3E
# channel as a live, clickable link signed by Cora.
#
# Same shape for platform tokens: a channel-sweep block whose status text mentions
# a data-SOURCE name would print it straight onto a Slack surface, defeating the
# label discipline the YAML-side test appeared to guarantee.
#
# Mirrors tool_dispatch._dash_scrub, which exists for exactly this on the other
# dashboard readers (D-051, 2026-07-11).
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
# `deposco` joined this list with the warehouse view (2026-08-14): it is the WMS
# we read the 3PL figures out of, i.e. a data-SOURCE name, and the same rule that
# keeps "shopify" off a Slack surface applies to it. The FACILITY (Nimbl) and the
# sales channels stay sayable -- those are operational facts, not tooling.
_VENDOR_RE = re.compile(
    r"\b(shopify|seller\s?cent(?:ral|er)|polar|airtable|quickbooks|notion|deposco)\b",
    re.IGNORECASE,
)


def founder_os_root() -> Path:
    env = os.environ.get("FOUNDER_OS_ROOT", "").strip()
    return Path(env) if env else Path(r"G:\My Drive\HJR-Founder-OS")


def store_dir() -> Path:
    return founder_os_root() / STORE_RELDIR


def store_path(source: str) -> Path:
    return store_dir() / STORE_FILES[source]


def last_good_path(source: str) -> Path:
    return store_dir() / (STORE_FILES[source] + ".last-good")


def scrub(text: Any, cap: int = 80) -> str:
    """Neutralize a string this module did not author, before it can reach Slack.

    D-118: SKU labels, location names, block statuses and manual-count notes are
    all externally authored (YAML seeds, Airtable free text, marketplace
    catalogs, Cowork-written JSON).

    Strips, in order: whitespace/newlines (so nothing breaks out of a rendered
    line), URLs (ALL hosts -- see the _URL_RE comment for why egress does not
    cover this), data-source/platform names, and Slack control syntax. URLs are
    removed BEFORE the control-char strip so a `<url|label>` form cannot survive
    as bare label text.
    """
    flat = _WS_RE.sub(" ", str(text or "")).strip()
    flat = _URL_RE.sub("[link]", flat)
    flat = _VENDOR_RE.sub("[source]", flat)
    flat = _CONTROL_RE.sub("", flat)
    return flat[:cap]


def coerce_count(raw: Any) -> int | None:
    """Units as an int, or None when the value cannot be trusted as a number.

    None means UNPARSEABLE and must render as such. Never returns 0 for a bad
    value -- "0 units" and "we could not read this" are different facts, and
    conflating them is how a stockout gets invented or hidden.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if float(raw).is_integer() else None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else None


# ─────────────────────────────────────────────────────────────────────────────
# SKU map
# ─────────────────────────────────────────────────────────────────────────────

def load_sku_map(path: Path | None = None) -> dict[str, Any]:
    """Canonical SKU map. FAIL-SOFT: an unreadable map yields an empty registry,
    which makes every channel item render UNMAPPED rather than crashing a sweep."""
    target = path or SKU_MAP_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("inventory_state: SKU map unreadable (%s)", exc)
        return {"skus": {}, "channels": {}, "_error": str(exc)[:200]}
    if not isinstance(raw, dict):
        return {"skus": {}, "channels": {}, "_error": "SKU map root is not a mapping"}
    raw.setdefault("skus", {})
    raw.setdefault("channels", {})
    return raw


def channel_label(sku_map: dict[str, Any], channel: str) -> str:
    labels = sku_map.get("channels") or {}
    return scrub(labels.get(channel) or channel.replace("_", " ").title(), 40)


def display_name(sku_map: dict[str, Any], sku: str) -> str:
    entry = (sku_map.get("skus") or {}).get(sku) or {}
    return scrub(entry.get("display_name") or sku, 60)


# ─────────────────────────────────────────────────────────────────────────────
# Source loading (defensive -- see the module docstring)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SourceLoad:
    """One store file's load result."""
    source: str
    data: dict[str, Any] | None = None
    status: str = "ok"          # ok | missing | parse-error | unavailable
    detail: str = ""
    from_last_good: bool = False

    @property
    def usable(self) -> bool:
        return self.data is not None

    @property
    def as_of(self) -> str | None:
        return (self.data or {}).get("as_of_utc")


# INTERACTIVE mount budget. drive_io's defaults (10s timeout, 90s retry) are sized
# for scheduled jobs; context_loader overrides them on the request path for the
# same reason we must here. Without the override a hung G: made ONE load_source
# take ~98s -- three of those blow the tool's own timeout, so the user got a
# generic "Tool timed out" instead of the honest all-UNKNOWN render this module
# works hard to produce. Worse, the long read trips drive_io's PROCESS-WIDE
# circuit breaker for 90s, fast-failing every other user's CLAUDE.md read.
_MOUNT_TIMEOUT_SEC = 2.0
_MOUNT_RETRY_SEC = 0.0


def _read_json(path: Path, reader: Any) -> tuple[dict[str, Any] | None, str, str]:
    """(payload, status, detail). Never raises."""
    # Passed as kwargs so a test double with a simpler signature still works.
    budget: dict[str, float] = {
        "timeout": _MOUNT_TIMEOUT_SEC, "retry_seconds": _MOUNT_RETRY_SEC,
    }
    try:
        try:
            exists = reader.exists(path, **budget)
        except TypeError:
            exists = reader.exists(path)
        if not exists:
            return None, "missing", "not written yet"
        try:
            text = reader.read_text(path, **budget)
        except TypeError:
            text = reader.read_text(path)
    except Exception as exc:  # noqa: BLE001 -- a dead mount must not kill the merge
        return None, "unavailable", f"{type(exc).__name__}: {exc}"[:160]
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, "parse-error", str(exc)[:160]
    if not isinstance(payload, dict):
        return None, "parse-error", "payload is not a JSON object"
    return payload, "ok", ""


def load_source(source: str, reader: Any = None) -> SourceLoad:
    """Load one store file, falling back to its `.last-good` sibling on a parse
    failure. A Cowork writer cannot guarantee atomic writes, so a torn or
    half-written file is an expected state, not an exceptional one."""
    if reader is None:
        from . import drive_io  # noqa: PLC0415
        reader = drive_io

    payload, status, detail = _read_json(store_path(source), reader)
    if status == "ok":
        return SourceLoad(source, payload, "ok")

    if status == "parse-error":
        salvage, lg_status, _ = _read_json(last_good_path(source), reader)
        if lg_status == "ok":
            log.warning("inventory_state: %s unparseable; using .last-good", source)
            return SourceLoad(source, salvage, "parse-error", detail, from_last_good=True)

    return SourceLoad(source, None, status, detail)


def promote_last_good(source: str, payload: dict[str, Any], writer: Any = None) -> None:
    """Persist a successfully-parsed payload as the `.last-good` fallback.

    Called by the merge step, NOT by the writers -- the point is that a good parse
    observed here is what makes a later torn write survivable.
    """
    if writer is None:
        from . import drive_io  # noqa: PLC0415
        writer = drive_io
    try:
        writer.write_text_atomic(
            last_good_path(source), json.dumps(payload, indent=2, sort_keys=True)
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort by design
        log.warning("inventory_state: could not update %s .last-good: %s", source, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Merge
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ChannelCount:
    channel: str
    units: int | None            # None = UNKNOWN / UNPARSEABLE, never 0
    as_of: str | None = None
    caveat: str = ""
    unparseable: bool = False
    #: Set when a Seller-Center block and the FBT mirror disagree materially.
    conflict: str = ""


@dataclass
class SkuRow:
    sku: str
    name: str
    counts: dict[str, ChannelCount] = field(default_factory=dict)
    mapped: bool = True

    @property
    def known_total(self) -> int | None:
        """Sum of the channels we could actually read, or None if we read none.

        Deliberately NOT a portfolio total: it is only meaningful next to the
        coverage footer, because unknown channels are omitted rather than zeroed.
        """
        known = [c.units for c in self.counts.values() if c.units is not None]
        return sum(known) if known else None

    @property
    def unknown_channels(self) -> list[str]:
        return sorted(c.channel for c in self.counts.values() if c.units is None)


@dataclass
class MergedInventory:
    rows: list[SkuRow] = field(default_factory=list)
    sources: dict[str, SourceLoad] = field(default_factory=dict)
    covered_channels: int = 0
    expected_channels: int = 0
    unmapped_items: list[dict[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (
            self.expected_channels > 0
            and self.covered_channels == self.expected_channels
            and all(s.usable and not s.from_last_good for s in self.sources.values())
        )


def _channel_blocks(load: SourceLoad, channels: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Per-channel sub-blocks from one source. Each carries its own as_of/status,
    so one dead marketplace login never blanks the others."""
    if not load.usable:
        return {}
    blocks = (load.data or {}).get("channels")
    if not isinstance(blocks, dict):
        return {}
    return {k: v for k, v in blocks.items() if k in channels and isinstance(v, dict)}


def merge(
    sku_map: dict[str, Any] | None = None,
    loads: dict[str, SourceLoad] | None = None,
    reader: Any = None,
) -> MergedInventory:
    """Merge the per-writer store files into per-SKU cross-channel rows."""
    smap = sku_map if sku_map is not None else load_sku_map()
    if loads is None:
        loads = {src: load_source(src, reader=reader) for src in STORE_FILES}

    merged = MergedInventory(sources=loads)
    known_skus = list((smap.get("skus") or {}).keys())

    shopify_blocks = _channel_blocks(loads.get("shopify", SourceLoad("shopify")), SHOPIFY_CHANNELS)
    sweep_blocks = _channel_blocks(loads.get("channels", SourceLoad("channels")), SWEEP_CHANNELS)
    manual_load = loads.get("manual", SourceLoad("manual"))

    all_channels = list(dict.fromkeys(SHOPIFY_CHANNELS + SWEEP_CHANNELS))
    merged.expected_channels = len(all_channels)
    merged.covered_channels = sum(
        1 for ch in all_channels
        if _block_is_live(shopify_blocks.get(ch)) or _block_is_live(sweep_blocks.get(ch))
    )

    manual_by_sku = _manual_by_sku(manual_load)

    for sku in known_skus:
        row = SkuRow(sku=sku, name=display_name(smap, sku))

        for channel in SHOPIFY_CHANNELS:
            block = shopify_blocks.get(channel)
            if channel == "tiktok_fbt":
                continue  # resolved below against the authoritative sweep
            row.counts[channel] = _count_from(block, sku, channel)

        # TikTok FBT: the marketplace-managed Seller-Center figure is preferred
        # over the mirror when it is fresher, but a material disagreement shows
        # BOTH with provenance rather than quietly picking one.
        row.counts["tiktok_fbt"] = _resolve_fbt(
            mirror=_count_from(shopify_blocks.get("tiktok_fbt"), sku, "tiktok_fbt"),
            authoritative=_count_from(sweep_blocks.get("tiktok_fbt"), sku, "tiktok_fbt"),
        )

        for channel in ("amazon_fba", "walmart_wfs"):
            row.counts[channel] = _count_from(sweep_blocks.get(channel), sku, channel)

        if sku in manual_by_sku:
            row.counts["manual"] = manual_by_sku[sku]

        merged.rows.append(row)

    merged.unmapped_items = _unmapped(sweep_blocks, known_skus)
    return merged


def _block_is_live(block: dict[str, Any] | None) -> bool:
    """A channel counts as READ only if it carries an actual SKU mapping.

    A block that is present and status-ok but has no `skus` payload is
    structurally blind -- counting it toward coverage lets a broken writer report
    "6 of 6 channels read" while contributing no data at all, which is exactly
    the all-clear-over-nothing D-117 exists to prevent.
    """
    if not block or block.get("status") not in (None, "ok"):
        return False
    return isinstance(block.get("skus"), dict) and bool(block["skus"])


def _count_from(block: dict[str, Any] | None, sku: str, channel: str) -> ChannelCount:
    caveat = CHANNEL_CAVEATS.get(channel, "")
    if not block:
        return ChannelCount(channel, None, None, caveat)
    if block.get("status") not in (None, "ok"):
        return ChannelCount(
            channel, None, block.get("as_of_utc"), caveat or scrub(block.get("status"), 40)
        )
    items = block.get("skus")
    if not isinstance(items, dict) or sku not in items:
        return ChannelCount(channel, None, block.get("as_of_utc"), caveat)
    units = coerce_count(items.get(sku))
    return ChannelCount(
        channel, units, block.get("as_of_utc"), caveat, unparseable=units is None
    )


def _resolve_fbt(mirror: ChannelCount, authoritative: ChannelCount) -> ChannelCount:
    """Prefer the Seller-Center block over the marketplace-managed mirror; show
    both when they disagree materially (the mirror is known to drift)."""
    if authoritative.units is None:
        return mirror
    if mirror.units is None:
        return authoritative
    if abs(authoritative.units - mirror.units) >= MIRROR_DISAGREEMENT_UNITS:
        authoritative.conflict = f"mirror reads {mirror.units:,}"
    return authoritative


def _manual_by_sku(load: SourceLoad) -> dict[str, ChannelCount]:
    """Manual counts, keyed by SKU. Free text is scrubbed; a non-numeric count
    renders UNPARSEABLE rather than being interpolated into a figure."""
    if not load.usable:
        return {}
    entries = (load.data or {}).get("counts")
    if not isinstance(entries, list):
        return {}
    out: dict[str, ChannelCount] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sku = scrub(entry.get("sku"), 40)
        if not sku:
            continue
        units = coerce_count(entry.get("count"))
        out[sku] = ChannelCount(
            "manual", units,
            entry.get("as_of") or (load.data or {}).get("as_of_utc"),
            caveat=scrub(entry.get("location") or "manual", 40),
            unparseable=units is None,
        )
    return out


def _unmapped(sweep_blocks: dict[str, dict[str, Any]], known: list[str]) -> list[dict[str, str]]:
    """Channel items with no SKU-map row. Surfaced, never dropped -- a dropped SKU
    reads as 'we hold none of it'."""
    out: list[dict[str, str]] = []
    for channel, block in sweep_blocks.items():
        items = block.get("skus")
        if not isinstance(items, dict):
            continue
        for sku in items:
            if sku not in known:
                out.append({"channel": channel, "sku": scrub(sku, 40)})
    return sorted(out, key=lambda d: (d["channel"], d["sku"]))


# ─────────────────────────────────────────────────────────────────────────────
# Rendering (channel names only -- never source/tool names)
# ─────────────────────────────────────────────────────────────────────────────

def render_units(count: ChannelCount) -> str:
    """UNPARSEABLE and UNKNOWN are DIFFERENT operational signals and must stay
    distinguishable: "the sweep never ran" vs "the sweep ran and wrote garbage".
    An earlier version also required a non-empty caveat here, which silently
    collapsed UNPARSEABLE into UNKNOWN on the four channels that carry no caveat
    -- including both marketplace lanes, where nobody can check by hand."""
    if count.unparseable:
        return "UNPARSEABLE"
    if count.units is None:
        return "UNKNOWN"
    return f"{count.units:,}"


def render_rows(merged: MergedInventory, sku_map: dict[str, Any] | None = None,
                skus: list[str] | None = None) -> list[str]:
    """Per-SKU cross-channel lines plus a coverage footer."""
    smap = sku_map if sku_map is not None else load_sku_map()
    lines: list[str] = []
    wanted = set(skus) if skus else None

    for row in merged.rows:
        if wanted and row.sku not in wanted:
            continue
        parts: list[str] = []
        for channel, count in row.counts.items():
            label = channel_label(smap, channel)
            text = f"{label} {render_units(count)}"
            notes = [n for n in (count.caveat, count.conflict) if n]
            if notes:
                text += f" ({'; '.join(scrub(n, 40) for n in notes)})"
            parts.append(text)
        total = row.known_total
        total_txt = "UNKNOWN" if total is None else f"{total:,}"
        suffix = ""
        if row.unknown_channels:
            suffix = f" — excludes {len(row.unknown_channels)} channel(s) not readable"
        lines.append(f"• {row.name}: " + " | ".join(parts) + f" — known total {total_txt}{suffix}")

    lines.append(_coverage_footer(merged))
    for item in merged.unmapped_items:
        lines.append(
            f"• UNMAPPED on {channel_label(smap, item['channel'])}: {item['sku']} "
            f"— no row in the SKU map, so it is reported but not merged"
        )
    return lines


def channel_totals(merged: MergedInventory) -> dict[str, dict[str, Any]]:
    """Per-channel roll-up across SKUs: ``{channel: {units, known_skus, unknown_skus}}``.

    ``units`` is None when NO sku could be read for that channel -- the channel is
    unread, which is a different fact from "the channel holds zero".
    """
    out: dict[str, dict[str, Any]] = {}
    for row in merged.rows:
        for channel, count in row.counts.items():
            bucket = out.setdefault(channel, {"units": None, "known_skus": 0, "unknown_skus": 0})
            if count.units is None:
                bucket["unknown_skus"] += 1
            else:
                bucket["units"] = (bucket["units"] or 0) + count.units
                bucket["known_skus"] += 1
    return out


def render_channel_summary(merged: MergedInventory,
                           sku_map: dict[str, Any] | None = None) -> str:
    """One compact line for a daily brief: units per CHANNEL, unread ones named.

    Channel names only -- never source or tool names.
    """
    smap = sku_map if sku_map is not None else load_sku_map()
    totals = channel_totals(merged)
    known_parts: list[str] = []
    unread: list[str] = []
    for channel in list(dict.fromkeys(SHOPIFY_CHANNELS + SWEEP_CHANNELS)) + ["manual"]:
        bucket = totals.get(channel)
        if not bucket:
            continue
        label = channel_label(smap, channel)
        if bucket["units"] is None:
            unread.append(label)
            continue
        notes = [n for n in (CHANNEL_CAVEATS.get(channel, ""),) if n]
        # A channel where SOME skus read and others did not is a PARTIAL total.
        # Printing it bare makes a SKU that vanished from the feed look identical
        # to one holding zero.
        if bucket["unknown_skus"]:
            notes.append(
                f"{bucket['known_skus']} of "
                f"{bucket['known_skus'] + bucket['unknown_skus']} SKUs read"
            )
        suffix = f" ({'; '.join(notes)})" if notes else ""
        known_parts.append(f"{label} {bucket['units']:,}{suffix}")

    if not known_parts:
        return "- Cross-channel inventory: no channel could be read (not zero -- unread)"
    line = "- Cross-channel inventory: " + " | ".join(known_parts)
    if unread:
        line += f" -- not yet swept: {', '.join(unread)} (UNKNOWN, not zero)"
    return line


#: Neutral, source-opaque names for the three store writers. The internal keys
#: ("shopify") are DATA-SOURCE names and must never reach a Slack surface -- the
#: label-discipline test caught the coverage footer leaking exactly that.
_SOURCE_LABELS: dict[str, str] = {
    "shopify": "warehouse + DTC feed",
    "channels": "marketplace sweep",
    "manual": "manual counts",
}


def source_label(source: str) -> str:
    return _SOURCE_LABELS.get(source, "feed")


# ─────────────────────────────────────────────────────────────────────────────
# Warehouse (3PL) view -- the Deposco-side store file
# ─────────────────────────────────────────────────────────────────────────────

#: Written by scripts/run_deposco_inventory_sync.py. Deliberately NOT in
#: STORE_FILES: that store models per-SALES-CHANNEL counts and this is a
#: per-FACILITY warehouse view, so registering it would inflate the merge's
#: `expected_channels` with a source that can never satisfy it.
WAREHOUSE_FILE = "f3e-inventory-deposco.json"

#: Default OFF. The Phase-1 gate is "figures reconcile against the warehouse UI
#: AND the manual weekly Sheet on 2 consecutive weekly checks" -- which cannot be
#: met in the session that builds this. So the consumer ships dark and Harrison
#: flips it once the gate clears (the D-087 operator-flag pattern).
WAREHOUSE_FLAG = "CORA_DEPOSCO_WAREHOUSE_LINE"

#: Measures worth a one-line brief, in render order.
_WAREHOUSE_MEASURES = ("totalOnHandQty", "atpQty")


def warehouse_enabled() -> bool:
    return os.environ.get(WAREHOUSE_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def warehouse_path() -> Path:
    return store_dir() / WAREHOUSE_FILE


def load_warehouse(reader: Any = None) -> SourceLoad:
    """Load the warehouse store file with the same defences as every other
    source: a dead mount, a missing file or a torn write degrade to a labelled
    UNKNOWN rather than to silence."""
    if reader is None:
        from . import drive_io  # noqa: PLC0415

        reader = drive_io
    payload, status, detail = _read_json(warehouse_path(), reader)
    return SourceLoad("warehouse", payload, status, detail)


def render_warehouse_line(load: SourceLoad | None = None, reader: Any = None) -> str:
    """One brief line of warehouse on-hand. Channel/facility names only.

    Refuses to present a figure it cannot vouch for, in four distinct ways, each
    of which would otherwise read as "the 3PL holds nothing":
      * unreadable store file  -> UNKNOWN, not zero
      * `status: failed`       -> the writer's own coverage floor already said so
      * a non-production stamp -> sandbox carries no inventory, so never show it
      * a missing measure      -> UNKNOWN for that SKU, not zero
    """
    load = load if load is not None else load_warehouse(reader=reader)
    if not load.usable:
        return "- Warehouse (3PL) on-hand: not readable (UNKNOWN, not zero)"

    data = load.data or {}
    if str(data.get("status") or "").lower() == "failed":
        return "- Warehouse (3PL) on-hand: last sync did not complete (UNKNOWN, not zero)"
    if str(data.get("env") or "").lower() != "prod":
        # A sandbox payload reaching this renderer would show zeroes as fact.
        return "- Warehouse (3PL) on-hand: non-production data withheld (UNKNOWN, not zero)"

    items = data.get("items")
    if not isinstance(items, dict) or not items:
        return "- Warehouse (3PL) on-hand: no items returned (UNKNOWN, not zero)"

    parts: list[str] = []
    for sku in sorted(items):
        block = items[sku] if isinstance(items.get(sku), dict) else {}
        measures = block.get("measures") if isinstance(block.get("measures"), dict) else {}
        on_hand = coerce_count(measures.get(_WAREHOUSE_MEASURES[0]))
        shown = f"{on_hand:,}" if on_hand is not None else "UNKNOWN"
        parts.append(f"{scrub(sku, 24)} {shown}")

    line = "- Warehouse (3PL) on-hand: " + " | ".join(parts)

    notes: list[str] = []
    coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    missing = coverage.get("missing")
    if isinstance(missing, list) and missing:
        notes.append(f"{len(missing)} SKU(s) not returned (UNKNOWN, not zero)")
    if data.get("truncated"):
        notes.append("results truncated -- partial")
    as_of = data.get("as_of_utc")
    if as_of:
        notes.append(f"as of {scrub(as_of, 32)}")
    if notes:
        line += f" -- {'; '.join(notes)}"
    return line


def _coverage_footer(merged: MergedInventory) -> str:
    bits = [f"_Read {merged.covered_channels} of {merged.expected_channels} channel(s)"]
    stale = []
    for source, load in sorted(merged.sources.items()):
        name = source_label(source)
        if not load.usable:
            stale.append(f"{name}: {load.status}")
        elif load.from_last_good:
            stale.append(f"{name}: last-good copy (current file unparseable)")
        elif load.as_of:
            stale.append(f"{name} as of {scrub(load.as_of, 32)}")
    if stale:
        bits.append("; ".join(stale))
    return "; ".join(bits) + "._"
