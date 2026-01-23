
import os
import pytest
from unittest.mock import MagicMock, patch

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"

@pytest.fixture(scope="module")
def test_client():
    from fastapi.testclient import TestClient
    
    # Patch DB init and engine creation relative to where they are used/defined
    with patch("lenny.core.db.init"), \
         patch("lenny.core.db.create_engine"):
        
        # Import app inside patch context to ensure side-effects during import logic are mocked
        from lenny.app import app
        yield TestClient(app)

@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_default_auth_mode_oauth(mock_get_items, test_client):
    """Test that default request without params results in OAuth mode links (no auth_mode param)."""
    mock_get_items.return_value = {}
    
    response = test_client.get("/v1/api/opds")
    assert response.status_code == 200
    data = response.json()
    
    # Check navigation for absence of auth_mode
    navs = data.get("navigation", [])
    assert navs, "Navigation should not be empty"
    for nav in navs:
        assert "auth_mode=direct" not in nav["href"]

@patch("lenny.core.api.LennyDataProvider.search")
@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_dynamic_auth_param_propagation(mock_get_items, mock_search, test_client):
    # Mock items so we don't exit early (which would go to _build_empty_feed and skip links)
    mock_item = MagicMock()
    # Ensure it works with _build_query_and_lenny_ids expecting 'lenny' attr or valid OLID
    mock_item.lenny.openlibrary_edition = 123
    mock_get_items.return_value = {123: mock_item}
    
    # Mock search so we don't crash later
    mock_resp = MagicMock()
    mock_resp.records = []
    mock_resp.get_search_url.return_value = "/v1/api/opds/search"
    mock_resp.page = 1
    mock_resp.total = 0
    mock_resp.limit = 50
    mock_search.return_value = mock_resp
    
    response = test_client.get("/v1/api/opds?auth_mode=direct")
    assert response.status_code == 200
    data = response.json()
    
    # Check navigation - STICKY!
    navs = data.get("navigation", [])
    assert navs, "Navigation should not be empty"
    for nav in navs:
        assert "auth_mode=direct" in nav["href"]
        
    # Check "Bookshelf" link in links (if present)
    links = data.get("links", [])
    # Filter for relevant links to ensure we aren't asserting on empty list if filtering yields nothing by mistake
    target_rels = {"http://opds-spec.org/shelf", "profile"}
    sticky_links = [l for l in links if l.get("rel") in target_rels]
    
    assert sticky_links, "Should have found shelf/profile links"
    
    for link in sticky_links:
        assert "auth_mode=direct" in link["href"]

@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_beta_flag_backward_compatibility(mock_get_items, test_client):
    """Test that ?beta=true still triggers direct mode logic."""
    mock_get_items.return_value = {}
    
    response = test_client.get("/v1/api/opds?beta=true")
    assert response.status_code == 200
    data = response.json()
    
    # Sticky navigation should be engaged
    navs = data.get("navigation", [])
    assert navs, "Navigation should not be empty"
    for nav in navs:
        assert "auth_mode=direct" in nav["href"]

@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_single_item_sticky_profile_link(mock_get_items, test_client):
    """Test that single book view gets sticky profile link."""
    # Placeholder for single item test logic
    pass
