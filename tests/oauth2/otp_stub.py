"""A stand-in for Open Library's OTP service, so the borrow flow can be walked
end to end without a human inbox.

    python3 tests/oauth2/otp_stub.py &
    OTP_SERVER=http://127.0.0.1:18311 make up        # or your own run command

Then sign in at the node with any address and the code below.

**This needs no change to Lenny and is not an auth bypass.** `OTP_SERVER` is an
ordinary config value (`lenny/configs/__init__.py`), and `OTP._post` builds
`f"{OTP_SERVER}{path}"` — so pointing it elsewhere is the mechanism the code
already has. Nothing here is reachable on a node whose `OTP_SERVER` is Open
Library, which is the default and what production sets. There is no flag that
could be left armed.

That matters because the alternative considered first was a dev-only
deterministic OTP inside `OTP.verify`, gated on a config flag. That would have
been an auth bypass living in production code, one misconfiguration away from
letting anyone sign in as any patron. This is strictly safer and strictly less
code: the safest bypass is the one you do not write.

Implements only the two endpoints Lenny calls, and only their success and
mismatch answers. It deliberately does NOT emulate Open Library's other
failures — rate limiting, stale credentials, `auth_service_unavailable` — so a
green run here is not evidence those paths work. `tests/test_otp_error_surfacing.py`
covers them with fixtures.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, urllib.parse

CODE = "123456"

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        if q.path == "/account/otp/issue":
            body = {"success": "issued", "email": params.get("email", [""])[0]}
        elif q.path == "/account/otp/redeem":
            got = params.get("otp", [""])[0]
            body = ({"success": "redeemed"} if got == CODE
                    else {"error": "otp_mismatch"})
        else:
            body = {"error": "not_found"}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", 18311), H).serve_forever()
