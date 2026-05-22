"""Pytest configuration and shared fixtures.

Sets up the minimum environment variables required by cora.config at import
time so test modules can import cora packages without a real .env file.

The dummy tokens are formatted to pass the prefix-validation rules in
config.py but will never authenticate against any real service.

Also clears SOCKS/HTTP proxy environment variables that the Cowork sandbox
injects — these cause anthropic/httpx to fail when instantiating clients even
when the client creation is under test mocks.
"""

import os


def pytest_configure(config):
    """Called by pytest before any test collection or execution begins.

    Sets dummy env vars so cora.config._load() succeeds, and clears
    proxy vars that interfere with the anthropic SDK in CI/sandbox envs.
    """
    # ── Required tokens (format must match config._PREFIX_RULES) ──────────────
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-dummy-token-for-ci")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-1-test-dummy-token-for-ci")
    os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret-for-ci")
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key-for-ci")

    # ── Proxy vars that break anthropic/httpx in sandbox/CI environments ──────
    # The Cowork sandbox sets all_proxy=socks5h://localhost:1080 which causes
    # anthropic.Anthropic() to try to configure SOCKS support and fail with
    # ImportError when 'socksio' is not installed.  Unset all proxy vars here;
    # tests that actually need network access should set them explicitly.
    for var in (
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
        "FTP_PROXY", "ftp_proxy",
        "GRPC_PROXY", "grpc_proxy",
        "RSYNC_PROXY",
        "DOCKER_HTTP_PROXY", "DOCKER_HTTPS_PROXY",
    ):
        os.environ.pop(var, None)
