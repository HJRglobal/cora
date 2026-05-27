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
import sys
import types
from unittest.mock import patch


def _install_fake_tiktoken() -> None:
    """Register a network-free tiktoken stub in sys.modules before any source
    module is imported.

    chunker.py calls tiktoken.get_encoding("cl100k_base") at module load time.
    In CI / sandbox environments the encoding file cannot be fetched from
    openaipublic.blob.core.windows.net (403 / network-blocked), which causes a
    collection error for any test that transitively imports chunker.

    The stub treats each Unicode code-point as one token (len(text) tokens),
    which is deterministic and sufficient for the chunker's correctness tests.
    The encode/decode pair is reversible for ASCII inputs so the hard-truncation
    path in chunk_text() also works correctly.
    """
    if "tiktoken" in sys.modules:
        return

    class _FakeEncoder:
        def encode(self, text, disallowed_special=()):
            return [ord(c) for c in text]

        def decode(self, tokens):
            return "".join(chr(t) for t in tokens)

    _encoder = _FakeEncoder()
    fake = types.SimpleNamespace(get_encoding=lambda name: _encoder)
    sys.modules["tiktoken"] = fake  # type: ignore[assignment]


_install_fake_tiktoken()


def _mock_slack_auth_test() -> None:
    """Prevent the Bolt App() constructor from making a live auth.test call.

    Bolt calls slack_sdk's auth.test immediately when App(token=...) is
    constructed.  In tests we use a dummy token, so that call would reach
    Slack's servers and fail.  This patch intercepts it at the SDK level and
    returns a minimal successful response so any test file that imports
    cora.app can do so safely without a network connection.

    The patcher is never stopped — the mock remains in effect for the whole
    pytest session.  Real Slack interaction is never needed in unit tests.
    """
    fake_response = {
        "ok": True,
        "url": "https://test.slack.com/",
        "user_id": "U_CORA_TEST",
        "team": "TestWorkspace",
        "user": "testbot",
        "team_id": "T_TEST",
        "bot_id": "B_TEST",
    }
    patcher = patch(
        "slack_sdk.web.client.WebClient.auth_test",
        return_value=fake_response,
    )
    patcher.start()


_mock_slack_auth_test()


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
