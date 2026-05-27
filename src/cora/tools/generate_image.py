"""generate_image.py — Slack handlers for PhotoRoom image generation tools.

Wired into tool_dispatch.py as:
  "f3_generate_image"   → _tool_f3_generate_image
  "f3_batch_image_run"  → _tool_f3_batch_image_run
  "f3_create_image"     → _tool_f3_create_image

Entity scope: all tools require F3E or FNDR channel.

f3_create_image flow:
  1. Accept brand + brief (plain English) from Slack
  2. spec_generator calls Claude → generates PhotoRoom background prompt from brand guidelines
  3. photoroom_client calls PhotoRoom API → PNG bytes
  4. PNG uploaded to Drive photoroom-outputs/ folder → webViewLink returned
  5. Cora posts the Drive link in Slack for Harrison / BDM review

Source-opacity: Slack output never mentions PhotoRoom, Shopify, Drive paths, or token values.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from ..connectors.photoroom_client import (
    BatchResults,
    GenerateResult,
    PhotoroomBudgetError,
    PhotoroomConfigError,
    PhotoroomError,
    batch_run,
    format_batch_results_for_slack,
    format_result_for_slack,
    run_spec,
)
from ..connectors.photoroom_specs import (
    ImageSpec,
    validate_spec,
)
from ..connectors import spec_generator

log = logging.getLogger(__name__)

# Entities allowed to invoke image generation tools
_ALLOWED_ENTITIES = frozenset({"F3E", "FNDR"})


# ---------------------------------------------------------------------------
# Drive download helper (no file-system I/O)
# ---------------------------------------------------------------------------

def _download_drive_json(file_id: str) -> dict:
    """Download a JSON file from Drive by file ID and return parsed dict.

    Uses drive_connector._build_drive_service() + Files.get(alt=media).
    Raises ValueError on auth failure, HTTP error, or JSON parse error.
    """
    try:
        from ..connectors.drive_connector import _build_drive_service  # lazy import
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
        import io
    except ImportError as exc:
        raise ValueError(f"Drive dependencies not available: {exc}") from exc

    try:
        service = _build_drive_service()
    except Exception as exc:
        raise ValueError(f"Drive auth failed: {exc}") from exc

    try:
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        raw = buf.getvalue().decode("utf-8")
    except HttpError as exc:
        raise ValueError(
            f"Drive download failed (HTTP {exc.resp.status}): {exc.reason}"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Drive download error: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Drive file {file_id!r} is not valid JSON: {exc}"
        ) from exc


def _download_drive_folder_specs(folder_id: str) -> list[dict]:
    """List all .json files in a Drive folder and download each as a dict.

    Returns list of (spec_dict, filename) tuples.
    Raises ValueError on auth/HTTP errors.
    """
    try:
        from ..connectors.drive_connector import _build_drive_service
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
        import io
    except ImportError as exc:
        raise ValueError(f"Drive dependencies not available: {exc}") from exc

    try:
        service = _build_drive_service()
    except Exception as exc:
        raise ValueError(f"Drive auth failed: {exc}") from exc

    # List JSON files in folder
    try:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and name contains '.json' and trashed=false",
                fields="files(id,name)",
                pageSize=100,
            )
            .execute()
        )
        files = resp.get("files", [])
    except HttpError as exc:
        raise ValueError(
            f"Drive folder list failed (HTTP {exc.resp.status}): {exc.reason}"
        ) from exc

    if not files:
        raise ValueError(
            f"No .json spec files found in Drive folder {folder_id!r}."
        )

    results = []
    for f in files:
        try:
            request = service.files().get_media(fileId=f["id"])
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            spec_dict = json.loads(buf.getvalue().decode("utf-8"))
            results.append((spec_dict, f["name"]))
        except Exception as exc:
            log.warning("Skipping Drive file %s (%s): %s", f["id"], f["name"], exc)

    if not results:
        raise ValueError(
            f"Could not download any spec files from folder {folder_id!r}."
        )

    return results


# ---------------------------------------------------------------------------
# Slack handler: f3_generate_image
# ---------------------------------------------------------------------------

def handle_f3_generate_image(
    slack_user_id: str,
    entity: str,
    tool_input: dict[str, Any],
) -> str:
    """Slack tool handler for f3_generate_image.

    Accepts spec (dict) OR spec_drive_file_id (str) + optional dry_run (bool).
    Returns source-opaque Slack mrkdwn string.
    """
    # --- Entity scope guard ---
    if entity not in _ALLOWED_ENTITIES:
        return (
            "Image generation is only available in F3 channels. "
            "Please use this tool from #f3-pure-launch, #f3e-leadership, or a similar F3 channel."
        )

    dry_run: bool = bool(tool_input.get("dry_run", False))
    spec_dict: dict | None = tool_input.get("spec")
    drive_file_id: str | None = tool_input.get("spec_drive_file_id")

    # --- Resolve spec ---
    if spec_dict is not None and drive_file_id:
        return "Provide either `spec` or `spec_drive_file_id`, not both."

    if spec_dict is None and not drive_file_id:
        return (
            "No spec provided. Pass `spec` (a JSON object) or "
            "`spec_drive_file_id` (a Drive file ID pointing to a spec JSON)."
        )

    if drive_file_id:
        try:
            spec_dict = _download_drive_json(drive_file_id)
        except ValueError as exc:
            return f"Could not load spec from Drive: {exc}"

    # --- Validate ---
    try:
        spec: ImageSpec = validate_spec(spec_dict)  # type: ignore[arg-type]
    except ValidationError as exc:
        return f"Spec validation failed: {exc}"
    except Exception as exc:
        return f"Spec validation error: {exc}"

    # --- Dry run ---
    if dry_run:
        return (
            f"Dry run — spec *{spec.spec_id}* is valid.\n"
            f"Would generate 1 image ({spec.output.size} {spec.output.format}). "
            f"Estimated cost: $0.10."
        )

    # --- Execute ---
    try:
        result: GenerateResult = run_spec(spec, dry_run=False)
    except PhotoroomBudgetError as exc:
        return f"Weekly budget cap reached — no image generated. ({exc})"
    except PhotoroomConfigError as exc:
        return f"Image generation not configured: {exc}"
    except PhotoroomError as exc:
        return f"Image generation failed: {exc}"
    except Exception as exc:
        log.exception("f3_generate_image unexpected error for spec %s", getattr(spec, "spec_id", "?"))
        return f"Unexpected error during image generation: {exc}"

    return format_result_for_slack(result)


# ---------------------------------------------------------------------------
# Slack handler: f3_batch_image_run
# ---------------------------------------------------------------------------

def handle_f3_batch_image_run(
    slack_user_id: str,
    entity: str,
    tool_input: dict[str, Any],
) -> str:
    """Slack tool handler for f3_batch_image_run.

    Downloads all .json spec files from a Drive folder, validates them,
    runs them in series via batch_run(), and returns a batch summary.
    """
    # --- Entity scope guard ---
    if entity not in _ALLOWED_ENTITIES:
        return (
            "Batch image generation is only available in F3 or Founder channels."
        )

    dry_run: bool = bool(tool_input.get("dry_run", False))
    folder_id: str = (tool_input.get("spec_folder_drive_id") or "").strip()

    if not folder_id:
        return "Missing required parameter: `spec_folder_drive_id`."

    # --- Download specs from folder ---
    try:
        raw_specs = _download_drive_folder_specs(folder_id)
    except ValueError as exc:
        return f"Could not load specs from Drive folder: {exc}"

    # --- Validate all specs (skip invalid with warning) ---
    valid_specs: list[ImageSpec] = []
    validation_errors: list[str] = []
    for spec_dict, filename in raw_specs:
        try:
            valid_specs.append(validate_spec(spec_dict))
        except (ValidationError, Exception) as exc:
            validation_errors.append(f"  • {filename}: {exc}")

    if not valid_specs:
        lines = ["No valid specs found in folder. Validation errors:"]
        lines.extend(validation_errors)
        return "\n".join(lines)

    # --- Dry run ---
    if dry_run:
        est_cost = len(valid_specs) * 0.10
        lines = [
            f"Dry run — *{len(valid_specs)} valid spec(s)* found.",
            f"Estimated cost: ${est_cost:.2f}.",
        ]
        if validation_errors:
            lines.append(f"\n*{len(validation_errors)} skipped (validation errors):*")
            lines.extend(validation_errors)
        return "\n".join(lines)

    # --- Execute batch ---
    try:
        results: BatchResults = batch_run(valid_specs, dry_run=False)
    except PhotoroomBudgetError as exc:
        return f"Weekly budget cap reached before batch started: {exc}"
    except PhotoroomConfigError as exc:
        return f"Image generation not configured: {exc}"
    except Exception as exc:
        log.exception("f3_batch_image_run unexpected error, folder %s", folder_id)
        return f"Unexpected error during batch run: {exc}"

    summary = format_batch_results_for_slack(results)

    # Append any load-time validation errors
    if validation_errors:
        summary += f"\n\n*{len(validation_errors)} spec(s) skipped at load time:*\n"
        summary += "\n".join(validation_errors)

    return summary


# ---------------------------------------------------------------------------
# Slack handler: f3_create_image (brief → Claude → PhotoRoom → Drive)
# ---------------------------------------------------------------------------

_VALID_BRANDS = frozenset({"pure", "mood", "energy"})
_VALID_SIZES = frozenset({"1920x900", "1080x1080", "1200x628", "1920x1080"})


def handle_f3_create_image(
    slack_user_id: str,
    entity: str,
    tool_input: dict[str, Any],
) -> str:
    """Slack tool handler for f3_create_image.

    Accepts:
      brand (str)         — "pure" | "mood" | "energy"
      brief (str)         — plain-English scene description
      output_size (str)   — optional, default "1920x900"
      main_image_url (str)— optional override for the product can image URL
      dry_run (bool)      — validate + preview prompt, no API call

    Returns source-opaque Slack mrkdwn with Drive link on success.
    """
    # --- Entity scope guard ---
    if entity not in _ALLOWED_ENTITIES:
        return (
            "Image generation is only available in F3 channels. "
            "Please use this tool from #f3-pure-launch or #f3e-leadership."
        )

    brand: str = (tool_input.get("brand") or "").lower().strip()
    brief: str = (tool_input.get("brief") or "").strip()
    output_size: str = (tool_input.get("output_size") or "1920x900").strip()
    main_image_url: str | None = tool_input.get("main_image_url")
    dry_run: bool = bool(tool_input.get("dry_run", False))

    # --- Validate inputs ---
    if not brand:
        return "Missing `brand`. Specify: `pure`, `mood`, or `energy`."
    if brand not in _VALID_BRANDS:
        return f"Unknown brand `{brand}`. Valid options: pure, mood, energy."
    if not brief:
        return "Missing `brief`. Describe the scene in plain English."
    if len(brief) < 10:
        return "Brief is too short. Describe the scene with at least a few words."
    if output_size not in _VALID_SIZES:
        valid = ", ".join(sorted(_VALID_SIZES))
        return f"Unknown output_size `{output_size}`. Valid: {valid}."

    # --- Generate spec via Claude ---
    try:
        image_spec = spec_generator.generate_spec_from_brief(
            brand=brand,
            brief=brief,
            output_size=output_size,
            main_image_url=main_image_url or None,
            requester=slack_user_id,
        )
    except ValueError as exc:
        return f"Could not generate image spec: {exc}"
    except Exception as exc:
        log.exception("f3_create_image: spec_generator failed brand=%s", brand)
        return f"Unexpected error generating spec: {exc}"

    # --- Dry run: show what would be sent to PhotoRoom ---
    if dry_run:
        bg = image_spec.background
        return (
            f"🔍 *Dry run — F3 {brand.capitalize()} image brief*\n\n"
            f"*Generated background prompt:*\n_{bg.prompt}_\n\n"
            f"*Negative prompt:* {bg.negative_prompt}\n"
            f"*Guidance:* {bg.guidance.scale}\n"
            f"*Output:* {image_spec.output.size} {image_spec.output.format}\n"
            f"*Filename:* `{image_spec.output.filename}`\n\n"
            "Confirm with `dry_run=false` to generate and save to Drive."
        )

    # --- Execute: PhotoRoom → Drive ---
    try:
        result: GenerateResult = run_spec(image_spec, dry_run=False)
    except PhotoroomBudgetError as exc:
        return f"Weekly budget cap reached — no image generated. ({exc})"
    except PhotoroomConfigError as exc:
        return f"Image generation not configured: {exc}"
    except PhotoroomError as exc:
        return f"Image generation failed: {exc}"
    except Exception as exc:
        log.exception("f3_create_image: run_spec failed spec_id=%s", image_spec.spec_id)
        return f"Unexpected error during image generation: {exc}"

    # --- Format response ---
    base = format_result_for_slack(result)
    # Append brief summary so the reviewer knows what they're looking at
    return (
        f"{base}\n"
        f"*Brief:* _{brief[:120]}_\n"
        f"*Brand:* F3 {brand.capitalize()}"
    )
