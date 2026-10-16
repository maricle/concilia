/* Interfaz movil de operadores. Archivo propio: la CSP de /m no permite JS en linea. */
(function () {
  "use strict";

  var LADO_MAXIMO = 2000;
  var CALIDAD_JPEG = 0.85;

  // Reduce una foto a ~2000 px de lado mayor (JPEG 0,85) antes de subirla: menos
  // datos moviles y una subida mas rapida. PDF o cualquier error: va el original.
  function reducir(archivo) {
    return new Promise(function (resolver) {
      if (!archivo.type || archivo.type.indexOf("image/") !== 0 || !window.createImageBitmap) {
        resolver(archivo);
        return;
      }
      createImageBitmap(archivo).then(function (imagen) {
        var escala = Math.min(1, LADO_MAXIMO / Math.max(imagen.width, imagen.height));
        if (escala === 1 && archivo.type === "image/jpeg") {
          resolver(archivo);
          return;
        }
        var lienzo = document.createElement("canvas");
        lienzo.width = Math.round(imagen.width * escala);
        lienzo.height = Math.round(imagen.height * escala);
        lienzo.getContext("2d").drawImage(imagen, 0, 0, lienzo.width, lienzo.height);
        lienzo.toBlob(function (blob) {
          resolver(blob ? new File([blob], "comprobante.jpg", { type: "image/jpeg" }) : archivo);
        }, "image/jpeg", CALIDAD_JPEG);
      }).catch(function () { resolver(archivo); });
    });
  }

  function reemplazarArchivo(input, archivo) {
    try {
      var transferencia = new DataTransfer();
      transferencia.items.add(archivo);
      input.files = transferencia.files;
    } catch (e) {
      // Navegador sin DataTransfer editable: se sube el original.
    }
  }

  function mostrarVistaPrevia(contenedor, archivo) {
    contenedor.hidden = false;
    contenedor.textContent = "";
    if (archivo.type && archivo.type.indexOf("image/") === 0) {
      var imagen = document.createElement("img");
      imagen.alt = "Vista previa del comprobante";
      imagen.src = URL.createObjectURL(archivo);
      contenedor.appendChild(imagen);
    } else {
      var nombre = document.createElement("p");
      nombre.textContent = archivo.name;
      contenedor.appendChild(nombre);
    }
  }

  var formulario = document.getElementById("form-comprobante");
  if (formulario) {
    var camara = document.getElementById("archivo-camara");
    var elegido = document.getElementById("archivo-elegido");
    var vista = document.getElementById("vista-previa");
    var boton = document.getElementById("boton-enviar");
    [camara, elegido].forEach(function (input) {
      input.addEventListener("change", function () {
        var otro = input === camara ? elegido : camara;
        otro.value = "";
        var archivo = input.files && input.files[0];
        if (!archivo) { boton.disabled = true; return; }
        mostrarVistaPrevia(vista, archivo);
        boton.disabled = true;
        reducir(archivo).then(function (reducido) {
          if (reducido !== archivo) { reemplazarArchivo(input, reducido); }
          boton.disabled = false;
        });
      });
    });
    formulario.addEventListener("submit", function () {
      boton.disabled = true;
      boton.textContent = boton.getAttribute("data-texto-enviando");
    });
  }
})();
