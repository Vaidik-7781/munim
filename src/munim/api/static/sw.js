// Offline shell only: the app page is cached; API calls always go to the network (money data is never served stale).
const SHELL = "munim-shell-v1";
self.addEventListener("install", e => e.waitUntil(caches.open(SHELL).then(c => c.add("/app"))));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.pathname === "/app") e.respondWith(fetch(e.request).catch(() => caches.match("/app")));
});
