/* jellydash service worker: caches the app shell so the installed app opens even when the
   Pi is unreachable (it then shows DISCONNECTED). API calls always go to the network. */

const CACHE_NAME = 'jellydash-__VERSION__';
/* ROOT is wherever this worker was registered from: '/' on the Pi directly, '/jellydash/' behind a path. */
const ROOT = new URL('./', self.location).pathname;
const SHELL = ['', 'static/style.css', 'static/app.js', 'manifest.webmanifest', 'static/icon-192.png', 'static/icon-512.png']
	.map((name) => ROOT + name);

self.addEventListener('install', (event) => {
	event.waitUntil(
		caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
	);
});

self.addEventListener('activate', (event) => {
	event.waitUntil(
		caches.keys()
			.then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
			.then(() => self.clients.claim())
	);
});

/* Only the dashboard's own files are handled; anything else on this origin (for example
   SearXNG served under /searxng through tailscale serve) goes straight to the network. */
function isShellRequest(url) {
	return SHELL.includes(url.pathname) || url.pathname.startsWith(ROOT + 'static/');
}

self.addEventListener('fetch', (event) => {
	const url = new URL(event.request.url);
	if (event.request.method !== 'GET' || url.origin !== self.location.origin || !isShellRequest(url)) {
		return;
	}
	event.respondWith(
		fetch(event.request)
			.then((response) => {
				if (response.ok) {
					const copy = response.clone();
					caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
				}
				return response;
			})
			.catch(() => caches.match(event.request).then((cached) => cached || (event.request.mode === 'navigate' && url.pathname === ROOT ? caches.match(ROOT) : undefined)))
	);
});
