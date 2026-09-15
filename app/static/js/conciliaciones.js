/* Mejora progresiva sobre /conciliaciones: la pagina ya funciona 100% con
   links/forms normales sin esto (ver conciliaciones.html) -- este script solo
   evita el reload completo al cambiar de banco/pagina/filtro, y arma el modal
   de "resumen del dia" + el drag&drop de subida. Vanilla JS + jQuery (ya
   cargado por el bundle de SB Admin 2), sin librerias nuevas. */
(function () {
  "use strict";

  var ctx = window.CONCILIACIONES_CONTEXTO || {};
  var estado = { fecha: ctx.fecha, banco: ctx.banco, page: 1, page_size: 25, estado: "", search: "", operador_id: "" };
  var debounceTimer = null;

  function formatoMonto(valor) {
    if (valor === null || valor === undefined) return "-";
    var num = parseFloat(valor);
    if (isNaN(num)) return "-";
    var partes = num.toFixed(2).split(".");
    partes[0] = partes[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return "$" + partes[0] + "," + partes[1];
  }

  function badgeMovimiento(estadoMov) {
    if (estadoMov === "pendiente") return '<span class="badge-pill-estado badge-pill-pendiente">Pendiente</span>';
    if (estadoMov === "con_diferencia") return '<span class="badge-pill-estado badge-pill-diferencia">Diferencia</span>';
    return '<span class="badge-pill-estado badge-pill-conciliado">Conciliado</span>';
  }

  function badgeBanco(estadoBanco) {
    if (estadoBanco === "sin_resumen") return '<span class="badge-pill-estado badge-pill-sinresumen">SIN RESUMEN</span>';
    if (estadoBanco === "a_revisar") return '<span class="badge-pill-estado badge-pill-a_revisar">A REVISAR</span>';
    if (estadoBanco === "error") return '<span class="badge-pill-estado badge-pill-error">ERROR</span>';
    return '<span class="badge-pill-estado badge-pill-conciliado">CONCILIADO</span>';
  }

  function escapeHtml(texto) {
    return $("<div>").text(texto == null ? "" : texto).html();
  }

  function actualizarLinkExportar() {
    var params = $.param({
      fecha: estado.fecha,
      banco: estado.banco,
      estado: estado.estado,
      search: estado.search,
      operador_id: estado.operador_id,
    });
    $("#link-exportar-movimientos").attr("href", "/conciliaciones/movimientos/exportar?" + params);
  }

  function cargarMovimientos() {
    actualizarLinkExportar();
    $.getJSON("/conciliaciones/movimientos.json", {
      fecha: estado.fecha,
      banco: estado.banco,
      page: estado.page,
      page_size: estado.page_size,
      estado: estado.estado,
      search: estado.search,
      operador_id: estado.operador_id,
    }).done(function (data) {
      var $tbody = $("#tbody-movimientos");
      if (!data.items.length) {
        $tbody.html('<tr><td colspan="7">No hay movimientos para este filtro.</td></tr>');
      } else {
        var filas = data.items.map(function (m) {
          return (
            "<tr>" +
            "<td>" + escapeHtml(m.fecha_hora) + "</td>" +
            "<td>" + escapeHtml(m.comprobante) + "</td>" +
            "<td>" + escapeHtml(m.titular) + "</td>" +
            '<td class="num">' + formatoMonto(m.monto) + "</td>" +
            "<td>" + escapeHtml(m.reparto_label) + "</td>" +
            "<td>" + badgeMovimiento(m.estado) + "</td>" +
            '<td class="text-nowrap"><a href="/comprobantes/' + m.id + '/editar" class="btn btn-sm btn-outline-primary" title="Editar"><i class="fas fa-pencil-alt"></i></a></td>' +
            "</tr>"
          );
        });
        $tbody.html(filas.join(""));
      }
      $("#paginacion-info").text("Mostrando " + data.items.length + " de " + data.total + " movimientos");
      renderPaginacion(data);
    });
  }

  function renderPaginacion(data) {
    var $ul = $("#paginacion-movimientos");
    $ul.empty();
    if (data.total_pages <= 1) return;
    for (var p = 1; p <= data.total_pages; p++) {
      var activo = p === data.page ? " active" : "";
      $ul.append(
        '<li class="page-item' + activo + '"><a class="page-link" href="#" data-page="' + p + '">' + p + "</a></li>"
      );
    }
  }

  function cargarPanel() {
    $.getJSON("/conciliaciones/panel.json", { fecha: estado.fecha, banco: estado.banco }).done(function (data) {
      if (!data.panel) return;
      var panel = data.panel;
      $("#kpi-comprobantes").text(panel.cantidad_comprobantes);
      $("#kpi-declarado").text(formatoMonto(panel.total_declarado));
      $("#kpi-banco").text(panel.total_banco === null ? "-" : formatoMonto(panel.total_banco));
      $("#kpi-diferencia").text(panel.diferencia === null ? "-" : formatoMonto(panel.diferencia));
      $("#panel-badge-estado").html(badgeBanco(panel.estado));
    });
  }

  $(document).on("click", "#bank-tab-strip .bank-tab", function (e) {
    e.preventDefault();
    var banco = $(this).data("banco");
    estado.banco = banco;
    estado.page = 1;
    $("#bank-tab-strip .bank-tab").removeClass("active");
    $(this).addClass("active");
    var url = "/conciliaciones?fecha=" + estado.fecha + "&banco=" + encodeURIComponent(banco);
    window.history.pushState({}, "", url);
    cargarPanel();
    cargarMovimientos();
  });

  $("#btn-filtros").on("click", function () {
    $("#filtros-movimientos").slideToggle(150);
  });

  $("#buscador-movimientos").on("input", function () {
    var valor = $(this).val();
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      estado.search = valor;
      estado.page = 1;
      cargarMovimientos();
    }, 300);
  });

  $("#filtro-estado").on("change", function () {
    estado.estado = $(this).val();
    estado.page = 1;
    cargarMovimientos();
  });

  $("#filtro-operador").on("change", function () {
    estado.operador_id = $(this).val();
    estado.page = 1;
    cargarMovimientos();
  });

  $(document).on("click", "#paginacion-movimientos .page-link", function (e) {
    e.preventDefault();
    estado.page = parseInt($(this).data("page"), 10);
    cargarMovimientos();
  });

  // Modal de resumen del dia: se carga al abrirse (BS4 evento "show").
  $("#modalResumenDia").on("show.bs.modal", function () {
    var $body = $("#contenido-resumen-dia");
    $body.html('<div class="text-muted">Cargando...</div>');
    $.getJSON("/conciliaciones/resumen-dia.json", { fecha: estado.fecha }).done(function (data) {
      var filas = data.bancos
        .map(function (b) {
          return (
            '<tr><td>' + escapeHtml(b.banco) + "</td>" +
            '<td class="num">' + (b.cantidad_comprobantes || 0) + "</td>" +
            '<td class="num">' + formatoMonto(b.total_declarado) + "</td>" +
            "<td>" + badgeBanco(b.estado) + "</td></tr>"
          );
        })
        .join("");
      var extra = data.sin_banco_cantidad
        ? '<p class="small text-muted mb-0">Sin banco identificado: ' +
          data.sin_banco_cantidad +
          " comprobantes por " +
          formatoMonto(data.sin_banco_total) +
          "</p>"
        : "";
      $body.html(
        '<table class="table table-sm table-bordered mb-2"><thead><tr><th>Banco</th><th class="num">Comprobantes</th><th class="num">Declarado</th><th>Estado</th></tr></thead><tbody>' +
          filas +
          "</tbody></table>" +
          extra
      );
      $("#btn-cerrar-dia").prop("disabled", !data.puede_cerrarse || data.cerrado);
    });
  });

  // Drag & drop del modal de subida.
  var $dropzone = $("#dropzone-resumen");
  var $input = $("#input-archivo-resumen");
  $dropzone.on("click", function (e) {
    // El input esta anidado dentro del dropzone: sin este chequeo, el click que
    // dispara .trigger("click") sobre el input burbujea de vuelta hasta este mismo
    // handler (target = input) y lo vuelve a disparar, cancelando el dialogo que
    // recien se habia abierto.
    if (e.target === $input[0]) {
      return;
    }
    $input.trigger("click");
  });
  $dropzone.on("dragover", function (e) {
    e.preventDefault();
    $dropzone.css("border-color", "var(--accent)");
  });
  $dropzone.on("dragleave drop", function () {
    $dropzone.css("border-color", "");
  });
  $dropzone.on("drop", function (e) {
    e.preventDefault();
    var files = e.originalEvent.dataTransfer.files;
    if (files && files.length) {
      $input[0].files = files;
      $("#nombre-archivo-resumen").text(files[0].name);
    }
  });
  $input.on("change", function () {
    if (this.files && this.files.length) {
      $("#nombre-archivo-resumen").text(this.files[0].name);
    }
  });
})();
