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


Colectores.get_original_global = colectar.get

if __name__ == "__main__":
    unittest.main()
