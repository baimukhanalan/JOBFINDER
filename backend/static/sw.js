// JobFinder service worker — enables install (PWA) + caches static assets. Navigations go
// straight to the network (the CRM must always be fresh; no stale mail served offline).
var CACHE = 'jf-static-v1';
self.addEventListener('install', function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){
    return c.addAll(['/static/logo.svg','/static/icon-192.png','/static/icon-512.png']);
  }).then(function(){ return self.skipWaiting(); }));
});
self.addEventListener('activate', function(e){
  e.waitUntil(caches.keys().then(function(keys){
    return Promise.all(keys.filter(function(k){return k!==CACHE;}).map(function(k){return caches.delete(k);}));
  }).then(function(){ return self.clients.claim(); }));
});
self.addEventListener('fetch', function(e){
  var req = e.request;
  if(req.method !== 'GET' || req.mode === 'navigate') return;   // never intercept nav/writes
  if(req.url.indexOf('/static/') !== -1){
    e.respondWith(caches.match(req).then(function(r){
      return r || fetch(req).then(function(resp){
        var copy = resp.clone(); caches.open(CACHE).then(function(c){ c.put(req, copy); });
        return resp;
      });
    }));
  }
});
