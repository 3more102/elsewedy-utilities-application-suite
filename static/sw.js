const CACHE='euas-shell-v3.9.0-ui8';
const ASSETS=[
  '/',
  '/static/styles.css',
  '/static/ui-refresh.css',
  '/static/ux-enhancements.css',
  '/static/productivity-enhancements.css',
  '/static/operational-enhancements.css',
  '/static/workspace-preferences.css',
  '/static/command-palette.css',
  '/static/app.js',
  '/static/ux-enhancements.js',
  '/static/dashboard-enhancements.js',
  '/static/productivity-enhancements.js',
  '/static/operational-enhancements.js',
  '/static/workspace-preferences.js',
  '/static/command-palette.js',
  '/static/manifest.webmanifest'
];

self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()));
});

self.addEventListener('activate',event=>{
  event.waitUntil(
    caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('euas-shell-')&&key!==CACHE).map(key=>caches.delete(key))))
      .then(()=>self.clients.claim())
  );
});

self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET'||event.request.url.includes('/api/')) return;
  event.respondWith(
    fetch(event.request).then(response=>{
      const copy=response.clone();
      caches.open(CACHE).then(cache=>cache.put(event.request,copy));
      return response;
    }).catch(()=>caches.match(event.request))
  );
});
