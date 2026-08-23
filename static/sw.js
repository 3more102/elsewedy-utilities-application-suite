const CACHE='euas-shell-v3.9.0-ui9';
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
  const request=event.request;
  if(request.method!=='GET') return;

  const url=new URL(request.url);
  const isShellRequest=url.origin===self.location.origin&&(url.pathname==='/'||url.pathname.startsWith('/static/'));
  if(!isShellRequest) return;

  event.respondWith((async()=>{
    try{
      const response=await fetch(request);
      if(response.ok&&response.type==='basic'){
        const cache=await caches.open(CACHE);
        await cache.put(request,response.clone());
      }
      return response;
    }catch(error){
      const cached=await caches.match(request);
      if(cached) return cached;
      if(request.mode==='navigate'){
        const shell=await caches.match('/');
        if(shell) return shell;
      }
      throw error;
    }
  })());
});
