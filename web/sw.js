// ponytail: network-first for everything; cache shell for offline open only
const SHELL = ['/', '/manifest.json', '/icon.svg'];
self.addEventListener('install', (e) =>
  e.waitUntil(caches.open('bz-v1').then((c) => c.addAll(SHELL))));
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request, {ignoreSearch: true})));
});
