/* Service worker de la interfaz de operadores. Se sirve en /m/sw.js para que su
   alcance sea /m/.

   Solo cachea el "app shell" estatico (CSS, JS, iconos) y la pagina sin
   conexion. Nunca cachea paginas autenticadas ni comprobantes, y no hay cola de
   envio offline: sin conexion no se guarda nada en el celular. */
"use strict";

var VERSION = "concilia-m-v2";
var APP_SHELL = [
  "/m/static/m.css",
  "/m/static/m.js",
  "/m/static/sin-conexion.html",
  "/m/static/iconos/icono-192.png",
  "/m/static/iconos/icono-512.png",
];

self.addEventListener("install", function (evento) {
  evento.waitUntil(
    caches.open(VERSION).then(function (cache) { return cache.addAll(APP_SHELL); }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (evento) {
  evento.waitUntil(
    caches.keys().then(function (nombres) {
      return Promise.all(nombres.filter(function (n) { return n !== VERSION; }).map(function (n) { return caches.delete(n); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (evento) {
  var pedido = evento.request;
  if (pedido.method !== "GET") { return; }
  var url = new URL(pedido.url);
  if (url.origin !== self.location.origin) { return; }

  if (url.pathname.indexOf("/m/static/") === 0) {
    // Estaticos: primero la cache, y si no esta, la red (sin guardar nada nuevo).
    evento.respondWith(caches.match(pedido).then(function (cacheado) { return cacheado || fetch(pedido); }));
    return;
  }
  if (pedido.mode === "navigate") {
    // Paginas: siempre la red; sin conexion, el aviso. Nunca se cachean.
    evento.respondWith(fetch(pedido).catch(function () { return caches.match("/m/static/sin-conexion.html"); }));
  }
});
