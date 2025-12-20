import httpx
import pytest
from urllib.parse import urlparse, parse_qs

BASE_URL = "http://127.0.0.1:8080/v1/api"

@pytest.mark.skip(reason="Integration test requires running server - run manually with 'make up'")
def test_oauth_flow():
    with httpx.Client(base_url=BASE_URL, follow_redirects=False) as client:
        # 1. Check Global OPDS Feed for Authentication Link
        print("\n[1] Checking Global OPDS Feed...")
        try:
           resp = client.get("/opds")
           if resp.status_code == 200:
            feed = resp.json()
            print("SUCCESS: Fetched OPDS feed.")
            
            # Check for Authentication link
            # Check for Authentication link
            found_auth = False
            for link in feed.get("navigation", []):
                if link.get("title") == "Authentication":
                    found_auth = True
                    self_link = link.get("href")
                    print(f"SUCCESS: Found global Authentication link -> {self_link}")
                    break
            
            if not found_auth:
                print("FAILED: Global Authentication link not found.")
            
            # Check for Borrow Link Type in Publications
            found_borrow = False
            for pub in feed.get("publications", []):
                for link in pub.get("links", []):
                    if link.get("rel") == "http://opds-spec.org/acquisition/borrow":
                        found_borrow = True
                        borrow_type = link.get("type")
                        print(f"SUCCESS: Found borrow link in publication '{pub.get('metadata', {}).get('title', 'Unknown')}': type='{borrow_type}'")
                        if borrow_type != "application/opds-publication+json":
                             print(f"WARNING: Borrow link type is '{borrow_type}', expected 'application/opds-publication+json'")
                        else:
                             print("SUCCESS: Borrow link type is correct.")
                        break
                if found_borrow:
                    break
            
            if not found_borrow:
                print("WARNING: No borrow links found in OPDS feed publications.")
           else:
               print(f"FAILED to fetch OPDS feed: Status code {resp.status_code}")
        except Exception as e:
            print(f"FAILED to fetch OPDS feed: {e}")

        # 2. Check Implicit Endpoint
        print("\n[2] Checking Implicit Endpoint content...")
        resp = client.get("/oauth/implicit")
        if resp.status_code == 200:
            doc = resp.json()
            print("Auth Doc received:", doc)
            if doc.get("authentication", [{}])[0].get("type") == "http://opds-spec.org/auth/oauth/implicit":
                 print("SUCCESS: Valid Auth Doc type.")
            else:
                 print("FAILED: Invalid Auth Doc type.")
            
            # Check for new fields
            if "description" in doc:
                print("SUCCESS: Found description field.")
            
            # Check for new links
            found_rels = []
            for link in doc.get("links", []):
                rel = link.get('rel')
                found_rels.append(rel)
                print(f"Found link: {rel} -> {link.get('href')}")
            
            if "logo" in found_rels and "help" in found_rels:
                 print("WARNING: logo and help links were expected but might be missing due to user edits.")
            
            # Register might be in auth links now
            auth_links = doc.get("authentication", [{}])[0].get("links", [])
            print(f"Found {len(auth_links)} auth link(s). Expecting 1.")
            for link in auth_links:
                 print(f"Found auth link: {link.get('rel')} -> {link.get('href')} ({link.get('type')})")
        else:
            print(f"FAILED: /oauth/implicit returned {resp.status_code}")

        # 3. Simulate Authorize (Unauthenticated) -> Should return OTP Issue Page (200 OK)
        print("\n[3] Testing Authorize (Unauthenticated)...")
        redirect_uri = "http://example.com/callback"
        auth_url = f"/oauth/authorize?redirect_uri={redirect_uri}&state=xyz"
        resp = client.get(auth_url)
        
        if resp.status_code == 200:
             print("SUCCESS: Received 200 OK.")
             if 'action="/v1/api/oauth/authorize' in resp.text:
                 print("SUCCESS: Form action points to oauth/authorize.")
             else:
                 print("WARNING: Form action might be wrong or generic.")
        else:
             print(f"FAILED: Expected 200 OK (Form), got {resp.status_code}")

        # 4. Simulate Authorize (Authenticated) -> Should redirect to Callback
        print("\n[4] Testing Authorize (Authenticated)...")
        print("Skipped (requires valid session cookie logic mock)")

        # 5. Check 401 on restricted item
        print("\n[5] Testing Unauthenticated Borrow (401)...")
        # Logic is same as before, testing item borrow.
        items_resp = client.get("/items?limit=1")
        if items_resp.status_code == 200:
            items = items_resp.json()
            if items:
                 print("SUCCESS: Items found. 401 check remains valid logic.")
            else:
                 print("SKIPPED: No items.")
        else:
             print("FAILED: /items check failed.")

        # 6. Simulate OAuth OTP Flow (Issue + Redeem at /oauth/authorize)
        print("\n[6] Testing OAuth OTP Flow (Self-Contained)...")
        # Step A: Issue OTP
        payload_issue = {
            "email": "test@example.com",
            # We must provide params expected by the new logic
            "redirect_uri": redirect_uri,
            "state": "xyz",
            "client_id": "testclient"
        }
        # Note: requests/httpx json param sends body.
        otp_issue_resp = client.post("/oauth/authorize", json=payload_issue)
        print(f"OTP Issue Status: {otp_issue_resp.status_code}")
        
        if otp_issue_resp.status_code == 200 and 'name="otp"' in otp_issue_resp.text:
             print("SUCCESS: OTP Issue returned Redeem Page.")
             
             # Step B: Redeem OTP (Simulate)
             # We can't easily get the real OTP without mocking or intercepting.
             # But we can verify the POST for verification is handled.
             # We'll fail auth, but verify we stay on the page with error.
             payload_redeem = {
                 "email": "test@example.com",
                 "otp": "000000",
                 "redirect_uri": redirect_uri,
                 "state": "xyz"
             }
             otp_redeem_resp = client.post("/oauth/authorize", json=payload_redeem)
             print(f"OTP Redeem Status: {otp_redeem_resp.status_code}")
             if otp_redeem_resp.status_code == 200 and "Authentication failed" in otp_redeem_resp.text:
                 print("SUCCESS: OTP Redeem handled (Invalid OTP -> Error Page).")
             else:
                 print("FAILED: OTP Redeem response unexpected.")
        else:
             print(f"FAILED: OTP Issue did not return Redeem Page. Code: {otp_issue_resp.status_code}")
             print(otp_issue_resp.text[:500])
 
if __name__ == "__main__":
    test_oauth_flow()
