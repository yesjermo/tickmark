// Tickmark service worker: makes the app installable and opens instantly from the home screen.
// The app shell (page, scripts, styles, icons) is cached; market data, orders and accounts (/api) always go to the
// network and are never cached, so nothing stale or private sits on the device.
const SHELL = "tickmark-shell-v2";   // bump to drop every older cached page and asset
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(["/", "/manifest.webmanifest", "/icon-192.png", "/icon.svg"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname === "/mcp"
      || url.pathname.startsWith("/oauth") || url.pathname.startsWith("/.well-known")) return;
  if (e.request.mode === "navigate") {      // pages: always the server's current page; the cached copy only when offline
    e.respondWith((async () => {
      try {
        const r = await fetch(e.request, { cache: "no-store" });
        if (r.ok) { const copy = r.clone(); caches.open(SHELL).then((c) => c.put("/", copy)); }   // copy before the page reads it
        return r;
      } catch {
        return (await caches.match("/")) || Response.error();
      }
    })());
    return;
  }
  if (url.pathname.startsWith("/assets/")) { // hashed build files never change: cache first
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); }
      return r;
    })));
  }
});
