"""Shopify blog-article ops (S1 of the F3E publish lane).

The tests that matter most here are the SOURCE-LEVEL pins. The lane's central
safety promise -- "a drafting job cannot publish, and only the human tap can" --
is a property of the code's shape, not of any runtime branch, so it is asserted
against the source. A promise in a docstring is not a property of the code.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors import shopify_client as sc

_SRC_PATH = pathlib.Path(inspect.getsourcefile(sc))
_SRC = _SRC_PATH.read_text(encoding="utf-8")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3energy.myshopify.com")
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "shpat_test")


def _resp(status=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.json.return_value = {} if json_body is None else json_body
    r.text = text
    return r


def _article(article_id="gid://shopify/Article/1", title="T", published=False,
             handle="t", blog_handle="learn"):
    return {
        "id": article_id, "title": title, "handle": handle, "summary": "s",
        "isPublished": published, "publishedAt": None, "tags": [],
        "blog": {"id": "gid://shopify/Blog/9", "handle": blog_handle, "title": "Learn"},
        "author": {"name": "F3 Energy Team"},
    }


# ---------------------------------------------------------------------------
# SOURCE-LEVEL PINS -- the write-safety contract
# ---------------------------------------------------------------------------


def _functions_containing(needle: str) -> set[str]:
    """Names of module-level functions whose source contains `needle`."""
    tree = ast.parse(_SRC)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(_SRC, node) or ""
            if needle in seg:
                out.add(node.name)
    return out


def test_create_article_has_no_publish_parameter():
    """A drafting job cannot stage a LIVE article because the argument does not
    exist -- not because a default happens to be False."""
    params = set(inspect.signature(sc.create_article).parameters)
    for forbidden in ("is_published", "published", "publish", "isPublished",
                      "publish_date", "publishDate"):
        assert forbidden not in params, forbidden
    assert params == {
        "blog_id", "title", "body_html", "summary", "tags", "author_name",
        "image_url", "image_alt",
    }


def test_only_publish_article_ever_sends_ispublished_true():
    """The single most important invariant in this lane."""
    owners = _functions_containing('"isPublished": True')
    assert owners == {"publish_article"}, owners


def test_create_article_sends_ispublished_false_literally():
    seg = inspect.getsource(sc.create_article)
    assert '"isPublished": False' in seg


def test_no_article_delete_or_redirect_helper_exists():
    """Canon: never delete or redirect a legacy article URL (it may already be
    cited by an AI engine); fix in place. So the capability is simply absent."""
    assert "articleDelete" not in _SRC
    assert "redirectNewHandle" not in _SRC
    assert not [n for n in dir(sc) if "delete" in n.lower() and "article" in n.lower()]


def test_publish_and_create_both_read_back():
    for fn in (sc.create_article, sc.publish_article):
        seg = inspect.getsource(fn)
        assert "get_article(" in seg, fn.__name__


def test_get_article_is_not_cached():
    """A cache hit on the read-back primitive would defeat the read-back."""
    seg = inspect.getsource(sc.get_article)
    assert "_cache_get" not in seg and "_cache_set" not in seg


# ---------------------------------------------------------------------------
# id / url helpers
# ---------------------------------------------------------------------------


def test_gid_helpers_accept_both_forms_and_are_idempotent():
    assert sc.article_gid("618441081152") == "gid://shopify/Article/618441081152"
    assert sc.article_gid("gid://shopify/Article/5") == "gid://shopify/Article/5"
    assert sc.blog_gid("97115373888") == "gid://shopify/Blog/97115373888"
    assert sc.blog_gid(sc.blog_gid("7")) == "gid://shopify/Blog/7"


@pytest.mark.parametrize("bad", ["", "   ", "not-an-id", "12x"])
def test_gid_helpers_refuse_junk(bad):
    with pytest.raises(sc.ShopifyConnectorError):
        sc.article_gid(bad)
    with pytest.raises(sc.ShopifyConnectorError):
        sc.blog_gid(bad)


def test_admin_url_uses_the_numeric_id(env):
    url = sc.article_admin_url("gid://shopify/Article/618441441600")
    assert url == ("https://admin.shopify.com/store/f3energy"
                   "/content/articles/618441441600")


def test_public_url_is_the_reader_facing_domain_not_the_admin_one():
    url = sc.article_public_url("learn", "how-to-read-an-energy-drink-label")
    assert url == "https://f3energy.com/blogs/learn/how-to-read-an-energy-drink-label"
    assert "myshopify" not in url


def test_public_url_is_empty_when_a_handle_is_missing():
    assert sc.article_public_url("", "x") == ""
    assert sc.article_public_url("learn", "") == ""


# ---------------------------------------------------------------------------
# create_article
# ---------------------------------------------------------------------------


def test_create_article_stages_unpublished_and_returns_the_readback(env):
    created = {"data": {"articleCreate": {
        "article": {"id": "gid://shopify/Article/77", "title": "T",
                    "isPublished": False},
        "userErrors": [],
    }}}
    readback = {"data": {"article": _article("gid://shopify/Article/77", "T", False)}}
    with patch.object(sc.requests, "post",
                      side_effect=[_resp(200, created), _resp(200, readback)]) as post:
        art = sc.create_article(blog_id="9", title="T", body_html="<p>b</p>")
    assert art["isPublished"] is False
    # The mutation payload itself must carry isPublished False.
    sent = post.call_args_list[0][1]["json"]["variables"]["article"]
    assert sent["isPublished"] is False
    assert sent["blogId"] == "gid://shopify/Blog/9"


def test_create_article_raises_on_user_errors(env):
    body = {"data": {"articleCreate": {
        "article": None,
        "userErrors": [{"field": "title", "message": "is too long"}],
    }}}
    with patch.object(sc.requests, "post", return_value=_resp(200, body)):
        with pytest.raises(sc.ShopifyConnectorError, match="is too long"):
            sc.create_article(blog_id="9", title="T", body_html="<p>b</p>")


def test_create_article_raises_when_the_readback_says_published(env):
    """The nightmare case: the write landed but came back LIVE. That must never
    read as a successful staging."""
    created = {"data": {"articleCreate": {
        "article": {"id": "gid://shopify/Article/77", "title": "T", "isPublished": False},
        "userErrors": [],
    }}}
    readback = {"data": {"article": _article("gid://shopify/Article/77", "T", True)}}
    with patch.object(sc.requests, "post",
                      side_effect=[_resp(200, created), _resp(200, readback)]):
        with pytest.raises(sc.ShopifyConnectorError, match="read-back FAILED"):
            sc.create_article(blog_id="9", title="T", body_html="<p>b</p>")


def test_create_article_raises_on_a_title_mismatch(env):
    created = {"data": {"articleCreate": {
        "article": {"id": "gid://shopify/Article/77", "title": "T", "isPublished": False},
        "userErrors": [],
    }}}
    readback = {"data": {"article": _article("gid://shopify/Article/77", "Other", False)}}
    with patch.object(sc.requests, "post",
                      side_effect=[_resp(200, created), _resp(200, readback)]):
        with pytest.raises(sc.ShopifyConnectorError, match="title mismatch"):
            sc.create_article(blog_id="9", title="T", body_html="<p>b</p>")


@pytest.mark.parametrize("kw", [
    {"title": "", "body_html": "<p>b</p>"},
    {"title": "T", "body_html": ""},
])
def test_create_article_refuses_empty_required_fields(env, kw):
    with pytest.raises(sc.ShopifyConnectorError):
        sc.create_article(blog_id="9", **kw)


# ---------------------------------------------------------------------------
# publish_article
# ---------------------------------------------------------------------------


def test_publish_article_flips_and_reads_back(env):
    pre = {"data": {"article": _article("gid://shopify/Article/77", "T", False)}}
    upd = {"data": {"articleUpdate": {
        "article": {"id": "gid://shopify/Article/77", "title": "T",
                    "isPublished": True, "publishedAt": "2026-08-26T00:00:00Z"},
        "userErrors": [],
    }}}
    post = {"data": {"article": _article("gid://shopify/Article/77", "T", True)}}
    with patch.object(sc.requests, "post",
                      side_effect=[_resp(200, pre), _resp(200, upd), _resp(200, post)]) as p:
        art = sc.publish_article("77")
    assert art["isPublished"] is True
    sent = p.call_args_list[1][1]["json"]["variables"]
    assert sent["article"] == {"isPublished": True}
    assert sent["id"] == "gid://shopify/Article/77"


def test_publish_article_is_idempotent_on_an_already_live_article(env):
    pre = {"data": {"article": _article("gid://shopify/Article/77", "T", True)}}
    with patch.object(sc.requests, "post", side_effect=[_resp(200, pre)]) as p:
        art = sc.publish_article("77")
    assert art["isPublished"] is True
    assert p.call_count == 1, "a second tap must not re-write a live article"


def test_publish_article_raises_when_the_readback_still_says_unpublished(env):
    """A silent no-op must never be reported as published."""
    pre = {"data": {"article": _article("gid://shopify/Article/77", "T", False)}}
    upd = {"data": {"articleUpdate": {
        "article": {"id": "gid://shopify/Article/77", "title": "T", "isPublished": True,
                    "publishedAt": None},
        "userErrors": [],
    }}}
    post = {"data": {"article": _article("gid://shopify/Article/77", "T", False)}}
    with patch.object(sc.requests, "post",
                      side_effect=[_resp(200, pre), _resp(200, upd), _resp(200, post)]):
        with pytest.raises(sc.ShopifyConnectorError, match="NOT live"):
            sc.publish_article("77")


def test_publish_article_raises_on_user_errors(env):
    pre = {"data": {"article": _article("gid://shopify/Article/77", "T", False)}}
    upd = {"data": {"articleUpdate": {
        "article": None,
        "userErrors": [{"field": "id", "message": "does not exist"}],
    }}}
    with patch.object(sc.requests, "post",
                      side_effect=[_resp(200, pre), _resp(200, upd)]):
        with pytest.raises(sc.ShopifyConnectorError, match="does not exist"):
            sc.publish_article("77")


# ---------------------------------------------------------------------------
# get_article / list_unpublished
# ---------------------------------------------------------------------------


def test_get_article_raises_when_absent(env):
    with patch.object(sc.requests, "post",
                      return_value=_resp(200, {"data": {"article": None}})):
        with pytest.raises(sc.ShopifyConnectorError, match="not found"):
            sc.get_article("77")


def test_list_unpublished_filters_on_the_authoritative_field(env):
    nodes = [
        _article("gid://shopify/Article/1", "live", True),
        _article("gid://shopify/Article/2", "draft", False),
        _article("gid://shopify/Article/3", "draft2", False),
    ]
    body = {"data": {"blog": {"id": "gid://shopify/Blog/9", "handle": "learn",
                              "title": "Learn", "articles": {"nodes": nodes}}}}
    with patch.object(sc.requests, "post", return_value=_resp(200, body)) as p:
        out = sc.list_unpublished("9")
    assert [a["title"] for a in out] == ["draft", "draft2"]
    q = p.call_args[1]["json"]["query"]
    # No sortKey: Blog.articles does not accept it at API 2024-10 (live-verified).
    assert "sortKey" not in q
    # reverse:true is load-bearing -- newest-first keeps fresh drafts in-window.
    assert "reverse: true" in q


def test_list_unpublished_raises_on_a_missing_blog(env):
    with patch.object(sc.requests, "post",
                      return_value=_resp(200, {"data": {"blog": None}})):
        with pytest.raises(sc.ShopifyConnectorError, match="not found"):
            sc.list_unpublished("9")


def test_list_unpublished_clamps_the_page_size(env):
    body = {"data": {"blog": {"handle": "learn", "articles": {"nodes": []}}}}
    with patch.object(sc.requests, "post", return_value=_resp(200, body)) as p:
        sc.list_unpublished("9", limit=9999)
    assert p.call_args[1]["json"]["variables"]["n"] == 250


# ---------------------------------------------------------------------------
# fetch_public_page
# ---------------------------------------------------------------------------


def _streamed(status=200, body=b"<h1>Hi</h1>"):
    """A streaming response. The fetch is STREAMED with a byte ceiling because it
    is also pointed at arbitrary third-party pages (see MAX_PAGE_BYTES)."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.encoding = "utf-8"
    resp.__enter__ = lambda self=resp: resp
    resp.__exit__ = lambda *a: False
    resp.iter_content = lambda chunk_size=65536: [body]
    return resp


def test_fetch_public_page_returns_status_and_body(env):
    with patch.object(sc.requests, "get", return_value=_streamed()):
        code, text = sc.fetch_public_page("https://f3energy.com/blogs/learn/x")
    assert code == 200 and "Hi" in text


def test_fetch_public_page_is_streamed_with_a_ceiling(env):
    with patch.object(sc.requests, "get", return_value=_streamed()) as get:
        sc.fetch_public_page("https://f3energy.com/blogs/learn/x")
    assert get.call_args[1]["stream"] is True
    assert sc.MAX_PAGE_BYTES > 0


def test_fetch_public_page_never_raises(env):
    """It runs AFTER a real publish. A crash there would lose the outcome."""
    with patch.object(sc.requests, "get",
                      side_effect=sc.requests.RequestException("boom")):
        code, text = sc.fetch_public_page("https://f3energy.com/blogs/learn/x")
    assert (code, text) == (0, "")
