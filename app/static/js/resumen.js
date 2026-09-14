/* Arma los dos graficos del home a partir de los datos que resumen.html inyecta
   en window.RESUMEN_DATA. Requiere Chart.js (cargado antes de este script). */
(function () {
  "use strict";
  var datos = window.RESUMEN_DATA;
  if (!datos || typeof Chart === "undefined") return;

  var ctxBarras = document.getElementById("chart-comprobantes-montos");
  if (ctxBarras) {
    new Chart(ctxBarras, {
      type: "bar",
      data: {
        labels: datos.dias.map(function (d) { return d.fecha; }),
        datasets: [
          {
            type: "bar",
            label: "Comprobantes",
            data: datos.dias.map(function (d) { return d.cantidad; }),
            backgroundColor: "#B9C4FF",
            yAxisID: "y",
            order: 2,
          },
          {
            type: "line",
            label: "Monto",
            data: datos.dias.map(function (d) { return d.monto; }),
            borderColor: "#6C5DD3",
            backgroundColor: "#6C5DD3",
            tension: 0.35,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { position: "left", title: { display: false }, grid: { color: "#EEEFF5" } },
          y1: { position: "right", grid: { drawOnChartArea: false } },
        },
        plugins: { legend: { position: "bottom" } },
      },
    });
  }

  var ctxDonut = document.getElementById("chart-comprobantes-banco");
  if (ctxDonut && datos.porBanco.length) {
    new Chart(ctxDonut, {
      type: "doughnut",
      data: {
        labels: datos.porBanco.map(function (b) { return b.banco; }),
        datasets: [
          {
            data: datos.porBanco.map(function (b) { return b.cantidad; }),
            backgroundColor: datos.porBanco.map(function (b) { return b.color; }),
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "68%",
        plugins: { legend: { display: false } },
      },
    });
  }
})();
