/* Los submenus de Conciliaciones y Configuracion en el sidebar (base.html) usan
   el collapse nativo de SB Admin 2, que por defecto se abre con click. Ademas
   del click (que sb-admin-2.min.js ya maneja via data-toggle="collapse", y que
   sigue funcionando para mobile/touch donde no hay hover), se agrega que se
   despliegen al pasar el mouse por encima, pedido explicito del usuario. */
(function () {
  "use strict";

  function activarHoverSubmenu(idMenu) {
    var $li = $("#" + idMenu);
    if (!$li.length) return;
    var $collapse = $li.find(".collapse").first();
    var timer = null;

    $li.on("mouseenter", function () {
      clearTimeout(timer);
      $collapse.collapse("show");
    });
    $li.on("mouseleave", function () {
      timer = setTimeout(function () {
        $collapse.collapse("hide");
      }, 200);
    });
  }

  $(function () {
    activarHoverSubmenu("menu-conciliaciones");
    activarHoverSubmenu("menu-configuracion");
  });
})();
