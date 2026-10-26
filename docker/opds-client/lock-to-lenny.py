"""Patch opds-client at build time so it only ever talks to this Lenny node.

Every feed/publication fetch goes through createFetchUrl; an off-origin target
(e.g. a hand-edited ?opds=https://other.example/opds) falls back to Lenny's own
feed, and sign-in redirects must stay on-origin. Off-origin rel="alternate" publications are skipped, not fetched.
"""
import sys

path = "src/lib/stores/opdsStore.ts"
src = open(path).read()

def swap(old, new):
    global src
    assert src.count(old) == 1, f"patch anchor not found exactly once: {old!r}"
    src = src.replace(old, new)

swap('export const feedUrl = "https://openlibrary.org/opds";',
     'export const feedUrl = (typeof window !== "undefined" ? window.location.origin : "http://localhost") + "/v1/api/opds";')
swap("""      return new URL(target, feedUrl).href;""",
     """      const resolved = new URL(target, feedUrl);
      return resolved.origin === new URL(feedUrl).origin ? resolved.href : feedUrl;""")
swap("""        if (fetchedLinks.has(link.href)) continue;""",
     """        if (fetchedLinks.has(link.href)) continue;
        try {
          if (new URL(link.href, feedUrl).origin !== new URL(feedUrl).origin) continue;
        } catch {
          continue;
        }""")
open(path, "w").write(src)

# Sign-in only ever redirects within this node. Lenny advertises relative
# authenticate hrefs, so resolve against our origin and refuse anything else.
path = "src/lib/auth.ts"
src = open(path).read()
swap("  const parsed = new URL(authenticateHref);",
     """  const parsed = new URL(authenticateHref, window.location.origin);
  if (parsed.origin !== window.location.origin) throw new Error("Refusing off-node sign-in");""")
open(path, "w").write(src)

# Lenny publishes Authorization Code + PKCE; the client only knows archive.org's
# client ids. Our own origin signs in as the node's built-in `lenny-opds-client`
# (registered by lenny.core.oauth2.ensure_default_clients).
src = open("src/lib/auth.ts").read()
swap("  const clientId = PKCE_CLIENT_IDS[window.location.origin];",
     '  const clientId = PKCE_CLIENT_IDS[window.location.origin] ?? "lenny-opds-client";')
open("src/lib/auth.ts", "w").write(src)
