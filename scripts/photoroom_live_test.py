"""
photoroom_live_test.py -- One-shot live test for the PhotoRoom orchestrator.

Uses a real F3 Pure Strawberry Lemonade can image from the Shopify CDN.
Destination: shopify_file_only (no Shopify wiring -- just validates the
PhotoRoom API call round-trip and saves the PNG locally).

Run from repo root:
    .venv\\Scripts\\python.exe scripts\\photoroom_live_test.py

Output: saves PNG to scripts/photoroom_live_test_output.png
"""

import sys
import time
from pathlib import Path

# Make sure src/ is on the path when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv()

from cora.config import config
from cora.connectors.photoroom_specs import validate_spec
from cora.connectors.photoroom_client import generate_ai_background, _api_key

# ---------------------------------------------------------------------------
# Test spec
# ---------------------------------------------------------------------------
# Real Strawberry Lemonade can image from Shopify CDN
REAL_CAN_URL = (
    "https://cdn.shopify.com/s/files/1/0747/7084/1920/files/"
    "F3_StrawberryLemonade_Front_Pure.png?v=1765900041"
)

spec_dict = {
    "spec_id": "live-test-pure-strawlemon-2026-05-26-001",
    "brand": "pure",
    "scene_name": "morning-walk-live-test",
    "feature": "ai_backgrounds",
    "main_image": {
        "type": "url",
        "value": REAL_CAN_URL,
    },
    "background": {
        "prompt": (
            "A woman in her early 30s on a sunlit morning walk through a tree-lined "
            "suburban neighborhood, golden hour light, warm and peaceful, natural green "
            "surroundings, soft bokeh background, lifestyle photography"
        ),
        "guidance": {"scale": 0.7},
        "negative_prompt": "text, watermark, logo overlay, dark scene, gym, office, indoors",
        "seed": None,
    },
    "output": {
        "format": "PNG",
        "size": "1920x900",
        "filename": "f3-pure-hero-morning-walk-live-test.png",
        "alt_text": "F3 Pure Strawberry Lemonade -- Lauren on morning walk",
    },
    "destination": {
        "type": "shopify_file_only",
        "shopify_target": {},
    },
    "metadata": {
        "requester": "harrison -- live test 2026-05-26",
        "tier": 1,
    },
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("PhotoRoom Live Test")
    print("=" * 60)

    # Config check
    try:
        key = _api_key()
        key_preview = key[:12] + "..."
        print(f"API key:    {key_preview}")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    sandbox = config.photoroom_use_sandbox
    base_url = config.photoroom_base_url
    print(f"Sandbox:    {sandbox}")
    print(f"Endpoint:   {base_url}/edit")
    print(f"Budget:     ${config.photoroom_weekly_budget_usd:.0f}/wk")
    print()

    if sandbox:
        print("WARNING: PHOTOROOM_USE_SANDBOX=true -- set to false in .env for live calls")
        print()

    spec = validate_spec(spec_dict)
    print(f"Spec:       {spec.spec_id}")
    print(f"Image URL:  {spec.main_image.value[:60]}...")
    print(f"Output:     {spec.output.size} {spec.output.format}")
    print()
    print("Calling PhotoRoom... (may take 10-30 seconds)")
    print()

    t0 = time.monotonic()
    try:
        png_bytes = generate_ai_background(spec)
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)

    elapsed = time.monotonic() - t0
    size_kb = len(png_bytes) / 1024

    print(f"SUCCESS in {elapsed:.1f}s")
    print(f"Output size: {size_kb:.1f} KB ({len(png_bytes):,} bytes)")
    print()

    # Save locally
    out_path = Path(__file__).parent / "photoroom_live_test_output.png"
    out_path.write_bytes(png_bytes)
    print(f"Saved to: {out_path}")
    print()
    print(f"Cost: $0.10 (this was a live call)")
    print("=" * 60)


if __name__ == "__main__":
    main()
