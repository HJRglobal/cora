"""Tests for the deterministic pre-LLM cross-entity guard."""

from cora.cross_entity_guard import check_cross_entity


def test_osn_channel_f3e_question_redirects():
    r = check_cross_entity("what's F3 Energy's monthly revenue?", "OSN")
    assert r is not None
    assert "#f3e-leadership" in r
    assert "F3 Energy" in r
    assert "scoped to One Stop Nutrition here" in r


def test_osn_channel_lex_question_redirects():
    r = check_cross_entity("how is the Lexington revalidation going?", "OSN")
    assert r is not None
    assert "#lex-leadership" in r
    assert "Lexington Services" in r


def test_f3e_channel_osn_question_redirects():
    r = check_cross_entity("what are One Stop Nutrition's store sales?", "F3E")
    assert r is not None
    assert "#osn-leadership" in r
    assert "scoped to F3 Energy here" in r


def test_f3e_channel_f3e_question_allows():
    r = check_cross_entity("how is F3 Pure inventory on Shopify?", "F3E")
    assert r is None


def test_fndr_channel_any_question_allows():
    assert check_cross_entity("what's F3 Energy's monthly revenue?", "FNDR") is None
    assert check_cross_entity("how is OSN doing?", "FNDR") is None
    assert check_cross_entity("Lexington revalidation status?", "FNDR") is None


def test_osn_channel_generic_greeting_allows():
    assert check_cross_entity("hey Cora, can you help me with something?", "OSN") is None


def test_lex_llc_channel_f3e_question_redirects():
    r = check_cross_entity("what's F3 Energy's revenue?", "LEX-LLC")
    assert r is not None
    assert "#f3e-leadership" in r
    assert "scoped to Lexington Services here" in r


def test_osngw_substore_f3e_question_redirects():
    r = check_cross_entity("how much did F3E make this month?", "OSNGW")
    assert r is not None
    assert "#f3e-leadership" in r
    assert "scoped to One Stop Nutrition here" in r


# --- Additional robustness checks ---------------------------------------


def test_hjrg_channel_passes_through():
    # #hjrg-* channels may ask portfolio-wide per founder doctrine.
    assert check_cross_entity("what's F3 Energy's revenue?", "HJRG") is None


def test_word_boundary_no_false_positive():
    # "villa" must not trigger the LLA (Lexington) redirect in an OSN channel.
    assert check_cross_entity("the customer lives in a nice villa", "OSN") is None
    # "results" must not trigger LTS.
    assert check_cross_entity("what were last month's results?", "OSN") is None


def test_empty_inputs_return_none():
    assert check_cross_entity("", "OSN") is None
    assert check_cross_entity("F3 Energy revenue?", "") is None


def test_ufl_question_in_bdm_channel_redirects():
    r = check_cross_entity("how is the United Fight League sponsor pipeline?", "BDM")
    assert r is not None
    assert "#ufl-leadership" in r
    assert "scoped to Big D Media here" in r
