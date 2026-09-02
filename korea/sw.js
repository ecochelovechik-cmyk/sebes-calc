/* Офлайн-режим приложения «Сеул → Uzum».
   Страница тяжёлая (фото товаров внутри файла), поэтому кэшируем её целиком
   при установке — дальше открывается мгновенно и без интернета.
   ВЕРСИЮ МЕНЯТЬ при каждом обновлении, иначе у Рашида останется старая копия. */

var CACHE = 'korea-v1';

var FILES = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './apple-touch-icon.png'
];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE)
      .then(function (c) { return c.addAll(FILES); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then(function (hit) {
      return hit || fetch(e.request).then(function (resp) {
        // фото докладываем в кэш по мере просмотра: класть все 235 при
        // установке — это лишние мегабайты на мобильном интернете
        if (resp && resp.ok && e.request.url.indexOf('/photos/') > -1) {
          var copy = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
        }
        return resp;
      }).catch(function () {
        return caches.match('./index.html');
      });
    })
  );
});
