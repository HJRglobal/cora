"""
photoroom_client.py — PhotoRoom AI Backgrounds API connector.

Reads image spec JSONs, calls PhotoRoom v2/edit, uploads PNG bytes to Shopify Files
via base64 inline fileCreate, and wires the file to its destination resource
(homepage section / collection hero / PDP hero / file-only).

All dev theme mutations target unpublished theme 185801638208 ONLY.
Never touches the published theme.

Spend is logged to logs/photoroom-spend.jsonl for budget governance.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import config
from ..connectors import shopify_client
from .photoroom_specs import (
    Background,
    BackgroundGuidance,
    Destination,
    ImageRef,
    ImageSpec,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

DEV_THEME_ID = "185801638208"


class PhotoroomError(Exception):
    """Base error for PhotoRoom connector."""


class PhotoroomConfigError(PhotoroomError):
    """API key or config missing."""


class PhotoroomAPIError(PhotoroomError):
    """Non-2xx response from PhotoRoom."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"PhotoRoom HTTP {status}: {body[:300]}")


class PhotoroomBudgetError(PhotoroomError):
    """Weekly budget cap exceeded."""


class ShopifyUploadError(PhotoroomError):
    """Image generated but Shopify upload failed."""


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class PhotoroomRateLimiter:
    """
    Token-bucket rate limiter.

    Tracks call timestamps in a fixed-size deque. If we've used all
    slots in the last 60 seconds, sleeps until the oldest call falls out
    of the window.
    """

    def __init__(self, calls_per_min: int = 60) -> None:
        self.calls_per_min = calls_per_min
        self.history: collections.deque[float] = collections.deque(maxlen=calls_per_min)

    def wait_if_needed(self) -> None:
        now = time.monotonic()
        # Expire entries older than 60 seconds
        while self.history and now - self.history[0] >= 60:
            self.history.popleft()
        if len(self.history) >= self.calls_per_min:
            sleep_for = 60 - (now - self.history[0]) + 0.1
            log.debug("PhotoRoom rate limit: sleeping %.1fs", sleep_for)
            time.sleep(sleep_for)
            self.wait_if_needed()
        self.history.append(now)


# Module-level singleton (shared across batch runs in the same process)
_rate_limiter = PhotoroomRateLimiter(calls_per_min=60)


# ---------------------------------------------------------------------------
# Budget governance
# ---------------------------------------------------------------------------

COST_PER_IMAGE_USD = 0.10
_SPEND_LOG_PATH = Path("logs/photoroom-spend.jsonl")

# In-memory weekly spend cache — reset when the log file rolls over Monday
_weekly_spend_cache: dict[str, float] = {}


def _iso_week_key() -> str:
    """Returns 'YYYY-Www' for the current UTC ISO week."""
    now = datetime.now(timezone.utc)
    return f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"


def _load_weekly_spend() -> float:
    """Sum spend from the JSONL log for the current ISO week."""
    week = _iso_week_key()
    if week in _weekly_spend_cache:
        return _weekly_spend_cache[week]
    total = 0.0
    if _SPEND_LOG_PATH.exists():
        for line in _SPEND_LOG_PATH.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if entry.get("week") == week and entry.get("status") == "ok":
                    total += float(entry.get("cost_usd", 0))
            except (json.JSONDecodeError, ValueError):
                pass
    _weekly_spend_cache[week] = total
    return total


def _log_spend(entry: dict[str, Any]) -> None:
    """Append a spend record to the JSONL log."""
    _SPEND_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _SPEND_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    # Invalidate cache
    _weekly_spend_cache.pop(_iso_week_key(), None)


def _check_budget(budget_usd: float) -> None:
    """Raise PhotoroomBudgetError if weekly spend already at or above budget."""
    spent = _load_weekly_spend()
    if spent >= budget_usd:
        raise PhotoroomBudgetError(
            f"Weekly budget hit (${spent:.2f} / ${budget_usd:.2f}). "
            "Reset Monday or raise PHOTOROOM_WEEKLY_BUDGET_USD in .env."
        )


# ---------------------------------------------------------------------------
# Image resolution helpers
# ---------------------------------------------------------------------------


def _resolve_image(ref: ImageRef) -> bytes:
    """
    Fetch raw image bytes from a reference.

    Supports:
    - shopify_file_url / url: HTTP GET
    - drive_file_id: Download from Google Drive via Drive connector
    """
    if ref.type in ("shopify_file_url", "url"):
        resp = httpx.get(ref.value, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    elif ref.type == "drive_file_id":
        # Import lazily to avoid circular deps; Drive connector lives in connectors/
        from ..connectors import drive_client  # type: ignore[attr-defined]
        return drive_client.download_file_bytes(ref.value)
    else:
        raise PhotoroomError(f"Unknown image ref type: {ref.type!r}")


# ---------------------------------------------------------------------------
# Core generation
# ---------------------------------------------------------------------------


def _photoroom_base_url() -> str:
    if config.photoroom_use_sandbox:
        return "https://sdk.photoroom.com/v1"  # sandbox endpoint
    return config.photoroom_base_url


def _api_key() -> str:
    key = config.photoroom_api_key
    if not key:
        raise PhotoroomConfigError(
            "PHOTOROOM_API_KEY not set. Add it to .env and restart Cora."
        )
    return key


def generate_ai_background(spec: ImageSpec) -> bytes:
    """
    Call PhotoRoom AI Backgrounds API.

    Sends the main product image + optional reference image + background parameters.
    Returns raw PNG/JPG/WebP bytes synchronously (typically 10-30 sec).

    Raises:
        PhotoroomConfigError: API key missing.
        PhotoroomAPIError: Non-2xx from PhotoRoom (401/422/429/5xx).
        httpx.TimeoutException: Generation took > 60 seconds.
    """
    headers = {"x-api-key": _api_key()}

    # Resolve main product image
    main_bytes = _resolve_image(spec.main_image)
    files: dict[str, Any] = {
        "imageFile": ("main.png", main_bytes, "image/png"),
    }

    # Optional reference image for background guidance
    if (
        spec.background.guidance
        and spec.background.guidance.image_ref
    ):
        ref_bytes = _resolve_image(spec.background.guidance.image_ref)
        files["background.guidance.imageFile"] = ("ref.png", ref_bytes, "image/png")

    data: dict[str, str] = {
        "background.prompt": spec.background.prompt,
        "outputSize": spec.output.size,
        "outputFormat": spec.output.format,
    }
    if spec.background.guidance:
        data["background.guidance.scale"] = str(spec.background.guidance.scale)
    if spec.background.negative_prompt:
        data["background.negativePrompt"] = spec.background.negative_prompt
    if spec.background.seed is not None:
        data["background.seed"] = str(spec.background.seed)

    url = f"{_photoroom_base_url()}/edit"
    log.info("PhotoRoom: generating '%s' (%s)", spec.spec_id, spec.scene_name)
    t0 = time.monotonic()

    try:
        r = httpx.post(url, headers=headers, files=files, data=data, timeout=60.0)
    except httpx.TimeoutException:
        raise PhotoroomAPIError(0, "Request timed out after 60s")

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "PhotoRoom: '%s' completed in %dms (HTTP %d)",
        spec.spec_id,
        elapsed_ms,
        r.status_code,
    )

    if r.status_code == 401:
        raise PhotoroomAPIError(r.status_code, "API key invalid. Check PHOTOROOM_API_KEY in .env.")
    if r.status_code == 400:
        raise PhotoroomAPIError(r.status_code, f"Bad request / unsafe content: {r.text}")
    if not r.is_success:
        raise PhotoroomAPIError(r.status_code, r.text)

    return r.content


# ---------------------------------------------------------------------------
# Shopify upload
# ---------------------------------------------------------------------------


def upload_to_shopify(png_bytes: bytes, alt: str, filename: str) -> str:
    """
    Upload image bytes to Shopify Files via base64 inline fileCreate.

    Works for files under ~5 MB (covers all F3 1920x900 hero PNGs).
    Returns the Shopify File GID, e.g. 'gid://shopify/MediaImage/...'.

    Raises ShopifyUploadError on Shopify-side failure.
    """
    b64 = base64.b64encode(png_bytes).decode()
    mutation = """
      mutation FileCreate($files: [FileCreateInput!]!) {
        fileCreate(files: $files) {
          files {
            id
            alt
            fileStatus
            ... on MediaImage {
              image { url }
            }
          }
          userErrors { field message }
        }
      }
    """
    variables = {
        "files": [
            {
                "originalSource": f"data:image/png;base64,{b64}",
                "alt": alt,
                "contentType": "IMAGE",
            }
        ]
    }

    result = shopify_client.graphql(mutation, variables)

    # Validate response
    user_errors = (
        result.get("data", {})
        .get("fileCreate", {})
        .get("userErrors", [])
    )
    if user_errors:
        msg = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
        raise ShopifyUploadError(f"Shopify fileCreate userErrors: {msg}")

    files_created = (
        result.get("data", {})
        .get("fileCreate", {})
        .get("files", [])
    )
    if not files_created:
        raise ShopifyUploadError("Shopify fileCreate returned no files")

    file_gid: str = files_created[0]["id"]
    log.info("Shopify upload OK: %s -> %s", filename, file_gid)
    return file_gid


# ---------------------------------------------------------------------------
# Wire-to-destination
# ---------------------------------------------------------------------------


def wire_to_destination(shopify_file_gid: str, spec: ImageSpec) -> None:
    """
    Wire an uploaded Shopify File to its target resource.

    All theme mutations target DEV_THEME_ID (unpublished) only.

    Destination types:
      - shopify_file_only: no-op after upload
      - pdp_hero: productCreateMedia + productReorderMedia
      - collection_hero: read+modify+upsert collection template JSON
      - homepage_hero_section: read+modify+upsert index template JSON
    """
    dest = spec.destination
    dt = dest.type
    st = dest.shopify_target
    filename = spec.output.filename

    if dt == "shopify_file_only":
        log.info("Destination is shopify_file_only — no theme wiring needed.")
        return

    if dt == "pdp_hero":
        _wire_pdp_hero(shopify_file_gid, st.product_handle, filename)

    elif dt == "collection_hero":
        _wire_collection_hero(shopify_file_gid, st.collection_handle, filename)

    elif dt == "homepage_hero_section":
        _wire_homepage_hero(shopify_file_gid, st.section_id, st.template_file, filename)

    else:
        raise PhotoroomError(f"Unknown destination type: {dt!r}")


def _wire_pdp_hero(file_gid: str, product_handle: Optional[str], filename: str) -> None:
    """Attach image to product at position 1 on the dev theme."""
    # Resolve product ID from handle
    query = """
      query ProductByHandle($handle: String!) {
        productByHandle(handle: $handle) { id title }
      }
    """
    result = shopify_client.graphql(query, {"handle": product_handle})
    product = result.get("data", {}).get("productByHandle")
    if not product:
        raise PhotoroomError(f"Product not found: {product_handle!r}")
    product_id = product["id"]

    # Attach media
    create_media_mutation = """
      mutation ProductCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
        productCreateMedia(productId: $productId, media: $media) {
          media { id mediaContentType status }
          mediaUserErrors { field message }
          product { id }
        }
      }
    """
    media_vars = {
        "productId": product_id,
        "media": [
            {
                "originalSource": file_gid,
                "mediaContentType": "IMAGE",
                "alt": filename,
            }
        ],
    }
    media_result = shopify_client.graphql(create_media_mutation, media_vars)
    media_errors = (
        media_result.get("data", {})
        .get("productCreateMedia", {})
        .get("mediaUserErrors", [])
    )
    if media_errors:
        msg = "; ".join(f"{e['field']}: {e['message']}" for e in media_errors)
        raise PhotoroomError(f"productCreateMedia errors: {msg}")

    created_media = (
        media_result.get("data", {})
        .get("productCreateMedia", {})
        .get("media", [])
    )
    if created_media:
        new_media_id = created_media[0]["id"]
        # Move to position 1
        reorder_mutation = """
          mutation ProductReorderMedia($id: ID!, $moves: [MoveInput!]!) {
            productReorderMedia(id: $id, moves: $moves) {
              job { id }
              userErrors { field message }
            }
          }
        """
        shopify_client.graphql(
            reorder_mutation,
            {"id": product_id, "moves": [{"id": new_media_id, "newPosition": "0"}]},
        )

    log.info("Wired PDP hero: product=%s file=%s", product_handle, file_gid)


def _wire_collection_hero(
    file_gid: str, collection_handle: Optional[str], filename: str
) -> None:
    """Update collection brand-header image in dev theme template JSON."""
    template_file = f"templates/collection.{collection_handle}.json"
    _upsert_theme_json_image(
        template_file=template_file,
        section_key="brand-header",
        image_setting_key="image",
        filename=filename,
    )
    log.info("Wired collection hero: %s -> %s", collection_handle, filename)


def _wire_homepage_hero(
    file_gid: str,
    section_id: Optional[str],
    template_file: Optional[str],
    filename: str,
) -> None:
    """Update or insert homepage hero section image in dev theme index.json."""
    tf = template_file or "templates/index.json"
    _upsert_theme_json_image(
        template_file=tf,
        section_key=section_id,
        image_setting_key="image",
        filename=filename,
    )
    log.info("Wired homepage hero: section=%s file=%s", section_id, filename)


def _upsert_theme_json_image(
    template_file: str,
    section_key: Optional[str],
    image_setting_key: str,
    filename: str,
) -> None:
    """
    Read a dev-theme template JSON, update the image setting, and push it back.

    Uses Shopify CDN reference format: 'shopify://shop_images/<filename>'
    All writes target DEV_THEME_ID only.
    """
    # Read current template from dev theme
    query = """
      query ThemeFile($themeId: ID!, $filenames: [String!]!) {
        theme(id: $themeId) {
          files(filenames: $filenames, first: 1) {
            nodes { filename body { ... on OnlineStoreThemeFileBodyText { content } } }
          }
        }
      }
    """
    theme_gid = f"gid://shopify/OnlineStoreTheme/{DEV_THEME_ID}"
    result = shopify_client.graphql(query, {"themeId": theme_gid, "filenames": [template_file]})

    nodes = (
        result.get("data", {})
        .get("theme", {})
        .get("files", {})
        .get("nodes", [])
    )
    if nodes and nodes[0].get("body", {}).get("content"):
        template_json = json.loads(nodes[0]["body"]["content"])
    else:
        # Create a minimal new template
        template_json = {"sections": {}, "order": []}

    # Update the target section
    sections = template_json.setdefault("sections", {})
    if section_key not in sections:
        sections[section_key] = {"type": section_key, "settings": {}}
    sections[section_key].setdefault("settings", {})[image_setting_key] = (
        f"shopify://shop_images/{filename}"
    )

    # Ensure section is in the order list
    order = template_json.setdefault("order", [])
    if section_key not in order:
        order.insert(0, section_key)

    # Push back to dev theme
    upsert_mutation = """
      mutation ThemeFilesUpsert($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) {
        themeFilesUpsert(themeId: $themeId, files: $files) {
          upsertedThemeFiles { filename }
          userErrors { field message }
        }
      }
    """
    upsert_vars = {
        "themeId": theme_gid,
        "files": [
            {
                "filename": template_file,
                "body": {"type": "TEXT", "value": json.dumps(template_json, indent=2)},
            }
        ],
    }
    upsert_result = shopify_client.graphql(upsert_mutation, upsert_vars)
    upsert_errors = (
        upsert_result.get("data", {})
        .get("themeFilesUpsert", {})
        .get("userErrors", [])
    )
    if upsert_errors:
        msg = "; ".join(f"{e['field']}: {e['message']}" for e in upsert_errors)
        raise PhotoroomError(f"themeFilesUpsert errors: {msg}")


# ---------------------------------------------------------------------------
# Orchestrator entry point
# ---------------------------------------------------------------------------


@dataclass
class GenerateResult:
    spec_id: str
    status: str  # "ok" | "error"
    shopify_file_gid: Optional[str] = None
    preview_url: Optional[str] = None
    cost_usd: float = 0.0
    cumulative_weekly_usd: float = 0.0
    duration_ms: int = 0
    error: Optional[str] = None


def run_spec(spec: ImageSpec, dry_run: bool = False) -> GenerateResult:
    """
    Full orchestration pipeline for one ImageSpec:
      1. Budget check
      2. Rate-limit wait
      3. PhotoRoom API call
      4. Shopify upload
      5. Wire to destination
      6. Spend log

    If dry_run=True: validates the spec + checks budget but skips all API calls.
    Returns cost estimate only.

    Raises PhotoroomBudgetError if weekly budget is exhausted.
    """
    budget = config.photoroom_weekly_budget_usd
    _check_budget(budget)

    if dry_run:
        spent = _load_weekly_spend()
        return GenerateResult(
            spec_id=spec.spec_id,
            status="dry_run",
            cost_usd=COST_PER_IMAGE_USD,
            cumulative_weekly_usd=spent,
        )

    _rate_limiter.wait_if_needed()
    t0 = time.monotonic()
    prompt_hash = "sha256:" + hashlib.sha256(spec.background.prompt.encode()).hexdigest()[:16]

    try:
        png_bytes = generate_ai_background(spec)
        file_gid = upload_to_shopify(
            png_bytes, spec.output.alt_text, spec.output.filename
        )
        wire_to_destination(file_gid, spec)

    except ShopifyUploadError as exc:
        # Image was generated but upload failed — save locally as rescue artefact
        rescue_dir = Path("logs/photoroom-failures")
        rescue_dir.mkdir(parents=True, exist_ok=True)
        rescue_path = rescue_dir / spec.output.filename
        try:
            # png_bytes may not be defined if exception was from a retry
            rescue_path.write_bytes(locals().get("png_bytes", b""))
        except Exception:
            pass
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_spend(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "spec_id": spec.spec_id,
                "brand": spec.brand,
                "feature": spec.feature,
                "status": "shopify_upload_failed",
                "duration_ms": elapsed_ms,
                "cost_usd": COST_PER_IMAGE_USD,
                "error": str(exc),
                "week": _iso_week_key(),
                "prompt_hash": prompt_hash,
            }
        )
        raise

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log_spend(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "spec_id": spec.spec_id,
                "brand": spec.brand,
                "feature": spec.feature,
                "status": "error",
                "duration_ms": elapsed_ms,
                "cost_usd": 0.0,
                "error": str(exc),
                "week": _iso_week_key(),
                "prompt_hash": prompt_hash,
            }
        )
        raise

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    dest_label = (
        f"{spec.destination.shopify_target.template_file or 'n/a'}"
        f"#{spec.destination.shopify_target.section_id or spec.destination.type}"
    )
    spent = _load_weekly_spend() + COST_PER_IMAGE_USD

    _log_spend(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "spec_id": spec.spec_id,
            "brand": spec.brand,
            "feature": spec.feature,
            "status": "ok",
            "duration_ms": elapsed_ms,
            "cost_usd": COST_PER_IMAGE_USD,
            "shopify_file_gid": file_gid,
            "shopify_destination": dest_label,
            "week": _iso_week_key(),
            "prompt_hash": prompt_hash,
        }
    )

    log.info(
        "run_spec OK: spec_id=%s gid=%s elapsed=%dms",
        spec.spec_id,
        file_gid,
        elapsed_ms,
    )
    return GenerateResult(
        spec_id=spec.spec_id,
        status="ok",
        shopify_file_gid=file_gid,
        cost_usd=COST_PER_IMAGE_USD,
        cumulative_weekly_usd=spent,
        duration_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


BATCH_HARD_CAP = 50  # max images per single batch run


@dataclass
class BatchResults:
    results: list[GenerateResult]

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results if r.status == "ok")


def batch_run(specs: list[ImageSpec], dry_run: bool = False) -> BatchResults:
    """
    Process N specs in series, respecting the rate limit.

    Hard cap: BATCH_HARD_CAP images per call to prevent runaway costs.
    Per-spec errors are captured and logged; the batch continues.
    """
    if len(specs) > BATCH_HARD_CAP:
        raise PhotoroomError(
            f"Batch size {len(specs)} exceeds hard cap of {BATCH_HARD_CAP}. "
            "Split into smaller batches."
        )

    results: list[GenerateResult] = []
    for spec in specs:
        try:
            result = run_spec(spec, dry_run=dry_run)
        except PhotoroomBudgetError:
            raise  # stop the whole batch on budget hit
        except Exception as exc:
            log.error("batch_run: spec %s failed: %s", spec.spec_id, exc)
            results.append(
                GenerateResult(spec_id=spec.spec_id, status="error", error=str(exc))
            )
            continue
        results.append(result)

    return BatchResults(results=results)


# ---------------------------------------------------------------------------
# Slack formatting helpers
# ---------------------------------------------------------------------------


def format_result_for_slack(result: GenerateResult) -> str:
    """Return a Slack mrkdwn summary for a single spec result."""
    if result.status == "dry_run":
        return (
            f"🔍 *Dry run* — `{result.spec_id}`\n"
            f"Would generate 1 image. Cost: ${result.cost_usd:.2f} | "
            f"Running this week: ${result.cumulative_weekly_usd:.2f} / "
            f"${config.photoroom_weekly_budget_usd:.2f} budget\n"
            "Confirm with `dry_run=false`."
        )
    if result.status == "ok":
        return (
            f"✅ Generated `{result.spec_id}`\n"
            f"💰 API cost: ${result.cost_usd:.2f} | "
            f"Cumulative this week: ${result.cumulative_weekly_usd:.2f} / "
            f"${config.photoroom_weekly_budget_usd:.2f} budget\n"
            f"⏱ {result.duration_ms:,}ms"
        )
    return f"❌ `{result.spec_id}` failed: {result.error}"


def format_batch_results_for_slack(results: BatchResults) -> str:
    """Return a Slack mrkdwn batch summary."""
    lines = [
        f"📊 *Batch complete* — {results.ok_count} ok / {results.error_count} errors",
        f"💰 Total cost: ${results.total_cost_usd:.2f}",
    ]
    for r in results.results:
        icon = "✅" if r.status == "ok" else "❌"
        lines.append(f"  {icon} `{r.spec_id}`" + (f" — {r.error}" if r.error else ""))
    return "\n".join(lines)
