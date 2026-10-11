import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def fake_edition_lookup(request):
    """Choosing an edition (PATCH olid) asks Open Library about it; tests never go out to the network.
    A test that cares patches `matcher.edition_facts` itself, or opts out with the `real_edition_lookup` marker."""
    if request.node.get_closest_marker("real_edition_lookup"):
        yield
        return
    def facts(olid):
        return {"olid": olid, "title": f"Edition {olid}", "authors": ["A. Writer"], "year": 2001,
                "publisher": "Pub", "language": "eng", "isbn": None}
    with patch("lenny.core.bulk_import.matcher.edition_facts", side_effect=facts):
        yield
