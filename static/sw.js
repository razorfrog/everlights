// Shows a friendly page instead of a blank screen when the app can't be reached
// (phone offline, or the server or tunnel at home down). Nothing else is cached:
// the app always loads fresh.
const CACHE = "everlights-offline-v1";
const OFFLINE = "/static/offline.html";

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add(OFFLINE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request)
    .then(r => (r.status >= 500 ? caches.match(OFFLINE).then(o => o || r) : r))
    .catch(() => caches.match(OFFLINE)));
});
