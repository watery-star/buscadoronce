"""Pruebas del colector con páginas de ejemplo (no usan internet).

Correr con:  python -m unittest colector/test_colectar.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import colectar  # noqa: E402

PROV_TN = {"id": "tn", "nombre": "Tienda", "url": "https://tienda.com", "plataforma": "tiendanube"}
PROV_SH = {"id": "sh", "nombre": "Shop", "url": "https://shop.com", "plataforma": "shopify"}
PROV_WC = {"id": "wc", "nombre": "Woo", "url": "https://woo.com", "plataforma": "woocommerce"}


class SinRobots:
    sitemaps = []
    estado = "leído"
    demora = 0

    def permite(self, url):
        return True


def falso_get(paginas):
    def get(url, intentos=3):
        if url not in paginas:
            from urllib.error import HTTPError
            raise HTTPError(url, 404, "no", {}, None)
        return paginas[url]
    return get


class Precios(unittest.TestCase):
    def test_formatos(self):
        casos = {"12800.00": 12800, "$ 12.800,00": 12800, "12.800": 12800, "1,5": 1.5,
                 "4920": 4920, 3500: 3500, "0": None, "": None, None: None, "1.234.567,89": 1234567.89,
                 "1,234.50": 1234.5}
        for entrada, esperado in casos.items():
            self.assertEqual(colectar.parse_precio(entrada), esperado, entrada)


class Extraccion(unittest.TestCase):
    def test_jsonld_tiendanube(self):
        html = """<html><head>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList",
          "itemListElement":[{"@type":"ListItem","position":1,"name":"Inicio","item":"https://tienda.com"},
          {"@type":"ListItem","position":2,"name":"Cocina","item":"https://tienda.com/cocina/"},
          {"@type":"ListItem","position":3,"name":"Termo acero 1L","item":"https://tienda.com/productos/termo/"}]}</script>
        <script type="application/ld+json">{"@context":"https://schema.org/","@type":"Product","name":"Termo acero 1L &amp; tapa",
          "image":"https://acdn.mitiendanube.com/x.jpg","description":"<p>Termo de acero inoxidable.</p><p>Mantiene 24 h.</p>",
          "offers":{"@type":"Offer","price":"12800.00","priceCurrency":"ARS","availability":"http://schema.org/InStock"}}</script>
        </head></html>"""
        it = colectar.extraer_producto(html, "https://tienda.com/productos/termo/", PROV_TN)
        self.assertEqual(it["n"], "Termo acero 1L & tapa")
        self.assertEqual(it["p"], 12800)
        self.assertEqual(it["c"], "Cocina")
        self.assertEqual(it["i"], "https://acdn.mitiendanube.com/x.jpg")
        self.assertEqual(it["d"], 1)
        self.assertEqual(it["t"], "Termo de acero inoxidable. Mantiene 24 h.")

    def test_jsonld_graph_y_agotado(self):
        html = """<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},
          {"@type":["Product"],"name":"Parlante","image":[{"url":"https://a/b.png"}],
           "offers":[{"@type":"AggregateOffer","lowPrice":9500,"availability":"https://schema.org/OutOfStock"}]}]}</script>"""
        it = colectar.extraer_producto(html, "https://x/p", PROV_TN)
        self.assertEqual((it["n"], it["p"], it["i"], it["d"]), ("Parlante", 9500, "https://a/b.png", 0))

    def test_opengraph(self):
        html = """<meta property="og:title" content="Cargador tipo C" />
        <meta property="og:image" content="https://img/c.jpg" />
        <meta property="product:price:amount" content="4920.00" />"""
        it = colectar.extraer_producto(html, "https://x/c", PROV_TN)
        self.assertEqual((it["n"], it["p"]), ("Cargador tipo C", 4920))

    def test_descripcion_larga_se_recorta(self):
        texto = "palabra " * 100
        r = colectar.recortar(texto)
        self.assertTrue(r.endswith("…"))
        self.assertLessEqual(len(r), 301)

    def test_pagina_sin_producto(self):
        self.assertIsNone(colectar.extraer_producto("<html>categoría</html>", "https://x", PROV_TN))


class Colectores(unittest.TestCase):
    def setUp(self):
        self.get_original = colectar.get
        colectar.PAUSA = 0

    def tearDown(self):
        colectar.get = self.get_original

    def test_shopify(self):
        pag1 = {"products": [{"title": "Vaso térmico", "handle": "vaso", "product_type": "Bazar", "body_html": "<b>500 ml</b>",
                              "images": [{"src": "https://cdn.shopify.com/v.jpg"}],
                              "variants": [{"price": "3500.00", "available": False},
                                           {"price": "3200.00", "available": True}]}]}
        colectar.get = falso_get({
            "https://shop.com/products.json?limit=250&page=1": json.dumps(pag1),
            "https://shop.com/products.json?limit=250&page=2": json.dumps({"products": []}),
        })
        r = colectar.shopify(PROV_SH, 100, SinRobots())
        self.assertEqual(r, [{"n": "Vaso térmico", "p": 3200, "u": "https://shop.com/products/vaso",
                              "i": "https://cdn.shopify.com/v.jpg", "c": "Bazar", "s": "sh", "d": 1, "t": "500 ml"}])

    def test_woocommerce_store_api(self):
        prods = [{"name": "Linterna &#8211; LED", "permalink": "https://woo.com/producto/linterna/",
                  "prices": {"price": "750000", "currency_minor_unit": 2},
                  "images": [{"src": "https://woo.com/wp-content/l.jpg"}],
                  "categories": [{"name": "Linterna"}], "is_in_stock": True}]
        colectar.get = falso_get({"https://woo.com/wp-json/wc/store/v1/products?per_page=100&page=1": json.dumps(prods)})
        r = colectar.woocommerce(PROV_WC, 100, SinRobots())
        self.assertEqual(r[0]["n"], "Linterna – LED")
        self.assertEqual(r[0]["p"], 7500)

    def test_woocommerce_sin_api_usa_sitemap(self):
        producto = '<script type="application/ld+json">{"@type":"Product","name":"Foco","offers":{"price":"1000"}}</script>'
        colectar.get = falso_get({
            "https://woo.com/sitemap.xml": "<sitemapindex><sitemap><loc>https://woo.com/post-sitemap.xml</loc></sitemap>"
                                           "<sitemap><loc>https://woo.com/product-sitemap.xml</loc></sitemap></sitemapindex>",
            "https://woo.com/product-sitemap.xml": "<urlset><url><loc>https://woo.com/producto/foco/</loc></url>"
                                                   "<url><loc>https://woo.com/wp-content/a.jpg</loc></url></urlset>",
            "https://woo.com/producto/foco/": producto,
        })
        r = colectar.woocommerce(PROV_WC, 100, SinRobots())
        self.assertEqual([(x["n"], x["p"]) for x in r], [("Foco", 1000)])

    def test_tiendanube_filtra_urls(self):
        prod = '<script type="application/ld+json">{"@type":"Product","name":"Mate","offers":{"price":"2000"}}</script>'
        colectar.get = falso_get({
            "https://tienda.com/sitemap.xml": "<urlset><url><loc>https://tienda.com/</loc></url>"
                                              "<url><loc>https://tienda.com/cocina/</loc></url>"
                                              "<url><loc>https://tienda.com/productos/mate/</loc></url></urlset>",
            "https://tienda.com/productos/mate/": prod,
        })
        r = colectar.sitemap_jsonld(PROV_TN, 100, SinRobots())
        self.assertEqual([x["u"] for x in r], ["https://tienda.com/productos/mate/"])


class FormatosNuevos(unittest.TestCase):
    def test_tipo_con_url_completa(self):
        # Deonce (CS-Cart) escribe el tipo como URL
        html = """<script type="application/ld+json">{"@context":"http://schema.org/","@type":"http://schema.org/Product",
          "name":"Toall\\u00f3n Racing","image":["https://deonce.com.ar/t.png"],
          "offers":[{"@type":"http://schema.org/Offer","availability":"InStock","price":10000,"priceCurrency":"ARS"}]}</script>"""
        it = colectar.extraer_producto(html, "https://deonce.com.ar/t-es/", PROV_TN)
        self.assertEqual((it["n"], it["p"], it["d"]), ("Toallón Racing", 10000, 1))

    def test_bloques_con_codigo_oestech(self):
        html = """<div class="caja_producto_destacado"><a href="#"><img class="img-responsive" src="fotos/99029.jpg"></a>
          <div class="datos_producto_destacado"> <h1>Código: 99029</h1> <h1>ESPEJO LED RECTANGULAR 17X29CM - - - - - - -</h1>
          <h1>Ctdad. por bulto: 36</h1> <h1>$9.141,60</h1> </div></div>
          <div class="caja_producto_destacado"><a href="#"><img src="fotos/AUR-K1.jpg"></a>
          <div> <h1>Código: AUR-K1</h1> <h1>(OE44037) AURICULAR BT K21 - - - -</h1> <h1>Ctdad. por bulto: 100</h1> <h1>$7.628,40</h1></div></div>"""
        prov = {"id": "oestech", "nombre": "Oestech", "url": "https://oestech.com.ar", "plataforma": "clave_php"}
        r = colectar.productos_por_codigo(html, "https://oestech.com.ar/", prov)
        self.assertEqual([(x["_codigo"], x["n"], x["p"], x["i"]) for x in r], [
            ("99029", "ESPEJO LED RECTANGULAR 17X29CM", 9141.6, "https://oestech.com.ar/fotos/99029.jpg"),
            ("AUR-K1", "(OE44037) AURICULAR BT K21", 7628.4, "https://oestech.com.ar/fotos/AUR-K1.jpg"),
        ])
        self.assertEqual(r[0]["t"], "Código: 99029 · Cantidad por bulto: 36")

    def test_tarjeta_oncepuntos(self):
        prov = {"id": "oncepuntos", "nombre": "Oncepuntos", "url": "https://oncepuntos.com.ar/", "plataforma": "navegador"}
        it = colectar.item_de_tarjeta(prov, {"nombre": "PILETA CON PARED RIGIDA 120X25 226L 51351", "precio": "$17.590",
                                             "categoria": "JUGUETERIA", "codigo": "CÓDIGO: 306648",
                                             "extra": "CANTIDAD POR BULTO: 1",
                                             "imagen": "https://placehold.co/400x400?text=Sin+imagen", "link": ""})
        self.assertEqual((it["p"], it["c"], it["i"], it["u"]), (17590, "Jugueteria", "", "https://oncepuntos.com.ar/#codigo-306648"))
        self.assertEqual(it["t"], "Código: 306648 · Cantidad por bulto: 1")

    def test_patron_propio_del_proveedor(self):
        colectar.get = falso_get({
            "https://deonce.com.ar/sitemap.xml": "<urlset><url><loc>https://deonce.com.ar/hogar/</loc></url>"
                                                 "<url><loc>https://deonce.com.ar/hogar/toalla-es/</loc></url></urlset>"})
        prov = {"id": "deonce", "url": "https://deonce.com.ar", "plataforma": "a_medida", "patron": "-es/$"}
        try:
            self.assertEqual(colectar.urls_de_productos(prov, SinRobots(), 100), ["https://deonce.com.ar/hogar/toalla-es/"])
        finally:
            colectar.get = Colectores.get_original_global


class CatalogosConClave(unittest.TestCase):
    OESTECH = """<div class="contenedor_producto"> <div class="col-xs-12 descrip"> <a name="ANTI-SPLASH" id="ANTI-SPLASH"></a> <!-- anclaje -->
      <h1>BEBEDERO ANTI SPLASH 22CM X 22CM</h1> </div> <div class="col-xs-12 foto"> <div class="carousel-inner"> <div class="item active">
      <a class="miniatura_imagen"> <img src="fotos/ANTI-SPLASH.jpg" data-codigo="ANTI-SPLASH"> </a> </div> </div> </div>
      <div class="col-xs-12" id="texto"> <p class="rubro_centrado">HOGAR</p><br> <p class="video_centrado"><a href="#"><img src="boton_video_falso.jpg"></a></p>
      <p class="datos1"><strong>Cód.:</strong> ANTI-SPLASH<br></p> <p class="datos1"> <strong>Cant. p/bulto:</strong> 32<br> </p>
      <div class="row mi_fila" id="fila_precios"> <p class="datos"><strong>$8.822</strong></p> </div>
      <form action="agregar_al_carrito.php?cod=ANTI-SPLASH" method="post"><input name="cant"></form> </div> </div>"""
    LAXSHI = """<div class="col-xs-12 caja_producto"> <div class="col-xs-12" id="descrip"> <a name="14763" id="14763"></a> <!-- anclaje -->
      <h1>17 EARPODS USB-C</h1> </div> <div class="col-xs-12 col-md-4" id="foto"> <a href="fotos/14763.jpg" target="_blank">
      <img class="img-responsive" src="fotos/14763.jpg"> </a> </div> <div class="col-xs-12 col-md-8" id="texto"> Codigo: 14763<br>
      Rubro: AURICULARES <br> Ctdad. por bulto: 1000<br> <br> <strong class="caja_precio_noimptk"> $ 2,323 </strong><br> </div> </div>"""

    def test_oestech_y_laxshi(self):
        p = {"id": "x", "nombre": "X", "url": "https://x", "plataforma": "clave_php"}
        o = colectar.productos_anclaje(self.OESTECH, "https://oestech.com.ar/catalogo2024.php?rub=99999", p)[0]
        self.assertEqual((o["n"], o["p"], o["c"], o["i"], o["u"], o["t"]),
                         ("BEBEDERO ANTI SPLASH 22CM X 22CM", 8822, "Hogar", "https://oestech.com.ar/fotos/ANTI-SPLASH.jpg",
                          "https://oestech.com.ar/catalogo2024.php?rub=99999#ANTI-SPLASH", "Código: ANTI-SPLASH · Cantidad por bulto: 32"))
        tak = """<a name="10248" id="10248"></a> <!-- anclaje --> <!-- <h1></h1> --> <h1>AAURICULAR BT NETMAK C/VISOR NM-BIZ ______________</h1>
          <img src="fotos/10248.jpg"> Codigo: 10248<br> Rubro: AURICULARES <br> Ctdad. por bulto: 100<br> <strong> $ 5,874 </strong>"""
        k = colectar.productos_anclaje(tak, "https://www.taktechnology.com.ar/catalogo2022x3.php?rub=99999", p)[0]
        self.assertEqual((k["n"], k["p"], k["c"]), ("AAURICULAR BT NETMAK C/VISOR NM-BIZ", 5874, "Auriculares"))
        l = colectar.productos_anclaje(self.LAXSHI + self.LAXSHI.replace("14763", "13012"), "https://laxshi.com/catalogo2021.php?rub=99999", p)
        self.assertEqual(len(l), 2)
        self.assertEqual((l[0]["n"], l[0]["p"], l[0]["c"], l[0]["i"], l[0]["t"]),
                         ("17 EARPODS USB-C", 2323, "Auriculares", "https://laxshi.com/fotos/14763.jpg", "Código: 14763 · Cantidad por bulto: 1000"))


class Prudencia(unittest.TestCase):
    def test_robots_bloqueado_y_sitemap_de_wordpress(self):
        from urllib.error import HTTPError
        colectar.get = lambda url, intentos=3: (_ for _ in ()).throw(HTTPError(url, 403, "no", {}, None))
        try:
            r = colectar.Robots("https://x.com")
        finally:
            colectar.get = Colectores.get_original_global
        self.assertFalse(r.permite("https://x.com/producto/a/"))
        self.assertTrue(r.estado.startswith("bloqueado"))
        colectar.get = falso_get({
            "https://woo.com/sitemap.xml": "<sitemapindex><sitemap><loc>https://woo.com/wp-sitemap-posts-post-1.xml</loc></sitemap>"
                                           "<sitemap><loc>https://woo.com/wp-sitemap-posts-product-1.xml</loc></sitemap></sitemapindex>",
            "https://woo.com/wp-sitemap-posts-product-1.xml": "<urlset><url><loc>https://woo.com/producto/foco/</loc></url></urlset>"})
        try:
            self.assertEqual(colectar.urls_de_productos(PROV_WC, SinRobots(), 10), ["https://woo.com/producto/foco/"])
        finally:
            colectar.get = Colectores.get_original_global


class Rastreo(unittest.TestCase):
    def test_deonce_por_categorias(self):
        prov = next(p for p in json.loads(Path(__file__).resolve().parent.parent.joinpath("proveedores.json").read_text(encoding="utf-8"))
                    if p["id"] == "deonce")
        tarjeta = '<div class="ut2-gl__name"> <a href="https://deonce.com.ar/hogar/{0}/" class="product-title" title="X">X</a></div>'
        colectar.get = falso_get({
            "https://deonce.com.ar/": '<a href="https://deonce.com.ar/hogar-y-blanco/">Hogar</a><a href="https://deonce.com.ar/carrito/">c</a>',
            "https://deonce.com.ar/hogar-y-blanco/": tarjeta.format("a") + '<a href="https://deonce.com.ar/hogar-y-blanco/page-2/">2</a>',
            "https://deonce.com.ar/hogar-y-blanco/page-2/": tarjeta.format("b") + tarjeta.format("a"),
        })
        colectar.PAUSA = 0
        try:
            urls = colectar.urls_de_productos(prov, SinRobots(), 100)
        finally:
            colectar.get = Colectores.get_original_global
        self.assertEqual(urls, ["https://deonce.com.ar/hogar/a/", "https://deonce.com.ar/hogar/b/"])


class CategoriasSinPaginar(unittest.TestCase):
    TARJETA = """<div class="itemWrapper col"> <div class="item item-first" id="item_3568467"> <div class="item-image">
      <a href="/catalogo/3568467" class="lnkGallery"> <img src="/slir/h270//files/x.jpeg" title="114121 - VASO" alt="VASO"> </a></div>
      <div class="item-details"> <h5 class="item-name"> VASO VIDRIO X 2 UNIDADES CERVECERO EN CAJA </h5>
      <span class="item-categories d-none d-md-block">Regaleria,Bazar,Vasos</span>
      <span class="reference-container" title="C&oacute;digo"><span class="item-info-line-title">C&oacute;digo:</span> <span class="reference">114121</span></span>
      <div class="item-description description">Set en caja de regalo.<br /> Material:Vidrio.</div>
      <div class="item-price price " data-view-item-price="9900.0000"> <span class="origPrice text-nowrap"> $ 9.900 </span> </div></div></div>"""

    def test_hipersarmiento(self):
        prov = next(p for p in json.loads(Path(__file__).resolve().parent.parent.joinpath("proveedores.json").read_text(encoding="utf-8"))
                    if p["id"] == "hipersarmiento")
        colectar.get = falso_get({
            "https://www.hipersarmiento.com.ar/": '<a href="/categoria/bazar">B</a><a href="/categoria/bazar%3Evasos">V</a>',
            "https://www.hipersarmiento.com.ar/categoria/bazar": self.TARJETA,
            "https://www.hipersarmiento.com.ar/categoria/bazar%3Evasos": self.TARJETA,
        })
        colectar.PAUSA = 0
        try:
            r = colectar.listado_html(prov, 100, SinRobots())
        finally:
            colectar.get = Colectores.get_original_global
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["n"], r[0]["p"], r[0]["c"], r[0]["u"]),
                         ("VASO VIDRIO X 2 UNIDADES CERVECERO EN CAJA", 9900, "Vasos", "https://www.hipersarmiento.com.ar/catalogo/3568467"))
        self.assertEqual(r[0]["t"], "Código: 114121 · Set en caja de regalo. Material:Vidrio.")

    def test_tarjeta_omega(self):
        prov = {"id": "omega", "nombre": "Omega", "url": "https://mayoristaomega.com/#catalogo", "plataforma": "navegador"}
        it = colectar.item_de_tarjeta(prov, {"nombre": "Kit de herramienta", "precio": "$ 5.900,00", "codigo": "Cod: CNW-22225",
                                             "imagen": "data:image/svg+xml,x", "link": "https://mayoristaomega.com/#producto/1253"})
        self.assertEqual((it["p"], it["i"], it["u"], it["t"]), (5900, "", "https://mayoristaomega.com/#producto/1253", "Código: CNW-22225"))

    def test_crawl_delay(self):
        colectar.get = lambda url, intentos=3: "User-agent: *\nCrawl-delay: 5\nDisallow: /*?*page=\n"
        try:
            r = colectar.Robots("https://x.com")
        finally:
            colectar.get = Colectores.get_original_global
        self.assertEqual(r.demora, 5)
        self.assertFalse(r.permite("https://x.com/categoria/bazar?page=2"))
        self.assertTrue(r.permite("https://x.com/categoria/bazar"))


class ProductosRelacionados(unittest.TestCase):
    def _pagina(self, con_url):
        def ld(nombre, precio, url=None):
            u = f'"url": "{url}",' if url else ""
            return ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",'
                    f'"name":"{nombre}",{u}"offers":{{"@type":"Offer","price":"{precio}","availability":"https://schema.org/InStock"}}}}</script>')
        return ('<meta property="og:title" content="Balanza Electronica 10kg"><meta property="product:price:amount" content="5200">'
                + ld("Plancha Silpat", 3620) + ld("Balanza Electronica 10kg", 5200, "https://x.com/productos/balanza/" if con_url else None))

    def test_toma_el_producto_de_la_pagina_por_url(self):
        prov = {"id": "x", "nombre": "X", "url": "https://x.com", "plataforma": "tiendanube"}
        it = colectar.extraer_producto(self._pagina(True), "https://x.com/productos/balanza/", prov)
        self.assertEqual((it["n"], it["p"]), ("Balanza Electronica 10kg", 5200))

    def test_toma_el_producto_de_la_pagina_por_titulo(self):
        prov = {"id": "x", "nombre": "X", "url": "https://x.com", "plataforma": "tiendanube"}
        it = colectar.extraer_producto(self._pagina(False), "https://x.com/productos/balanza/", prov)
        self.assertEqual((it["n"], it["p"]), ("Balanza Electronica 10kg", 5200))


class Listado(unittest.TestCase):
    TARJETA = """<div class="shop_item"> <ul class="slides"><li><a href="/producto/{slug}"><img loading="lazy" src="/uploads/principales/{slug}.png"></a></li></ul>
      <div class="shop_desc pb-0"> <div class="shop_title pull-left"> <a href="/producto/{slug}"> <span>{nombre}</span> </a>
      <span class="cats"> <a href="/categoria/bano">BAÑO</a> </span> </div> </div><div class="shop_desc pt-0"> <span class="price pull-left">
      <span style="cursor: pointer;" data-toggle="tooltip"> $2.376 - $3.393 </span> </span> </div></div>"""

    def test_sublink(self):
        prov = json.loads(Path(__file__).resolve().parent.parent.joinpath("proveedores.json").read_text(encoding="utf-8"))
        prov = next(p for p in prov if p["id"] == "sublink")
        pag1 = "".join(self.TARJETA.format(slug=f"p{i}", nombre=f"PRODUCTO {i}") for i in range(12))
        pag2 = self.TARJETA.format(slug="p12", nombre="DISPENSER CUADRADO VISOR")
        colectar.get = falso_get({"https://sublink.com.ar/tienda?page=1": pag1, "https://sublink.com.ar/tienda?page=2": pag2,
                                  "https://sublink.com.ar/tienda?page=3": pag2})
        colectar.PAUSA = 0
        try:
            r = colectar.listado_html(prov, 1000, SinRobots())
        finally:
            colectar.get = Colectores.get_original_global
        self.assertEqual(len(r), 13)
        self.assertEqual(r[-1], {"n": "DISPENSER CUADRADO VISOR", "p": 2376, "u": "https://sublink.com.ar/producto/p12",
                                 "i": "https://sublink.com.ar/uploads/principales/p12.png", "c": "Baño", "s": "sublink",
                                 "d": 1, "t": "Precios publicados: $2.376 a $3.393"})


Colectores.get_original_global = colectar.get

if __name__ == "__main__":
    unittest.main()
