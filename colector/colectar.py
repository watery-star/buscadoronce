#!/usr/bin/env python3
"""Colector de productos y precios de proveedores mayoristas de Once.

Lee proveedores.json, trae el catálogo de cada web según su plataforma y
escribe docs/productos.json, que es lo que usa el buscador.

Uso:
    python colector/colectar.py                 # todos los proveedores activos
    python colector/colectar.py --solo mzstore  # uno o varios (separados por coma)
    python colector/colectar.py --max 200       # límite de productos por proveedor (para probar)

Solo usa la biblioteca estándar de Python (3.9+).
"""
import argparse
import gzip
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from urllib.robotparser import RobotFileParser

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO_PROVEEDORES = RAIZ / "proveedores.json"
ARCHIVO_SALIDA = RAIZ / "docs" / "productos.json"

UA = "Mozilla/5.0 (compatible; BuscadorOnce/1.0; +https://github.com)"
TIMEOUT = 25
HILOS = 4            # pedidos simultáneos por proveedor (moderado, para no cargar las webs)
PAUSA = 0.25         # segundos entre pedidos de cada hilo
MAX_POR_DEFECTO = 15000
LIMITE_SIN_DATOS = 400  # si tantas páginas seguidas del sitemap no son productos (y no hubo ninguno), se corta

# Qué colector usa cada plataforma y qué URLs del sitemap son productos.
PATRONES_PRODUCTO = {
    "tiendanube": r"/productos/[^/?#]+/?$",
    "woocommerce": r"/(producto|product)/[^/?#]+/?$",
}


# ---------------------------------------------------------------- red

def get(url, intentos=3):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept-Language": "es-AR,es;q=0.9",
    })
    ultimo = None
    for intento in range(intentos):
        try:
            with urlopen(req, timeout=TIMEOUT) as r:
                datos = r.read()
                if datos[:2] == b"\x1f\x8b":
                    datos = gzip.decompress(datos)
                charset = r.headers.get_content_charset() or "utf-8"
                return datos.decode(charset, errors="replace")
        except HTTPError as e:
            if e.code in (401, 403, 404, 410):
                raise
            ultimo = e
        except (URLError, TimeoutError, ConnectionError, OSError) as e:
            ultimo = e
        time.sleep(2 * (intento + 1))
    raise ultimo


class Robots:
    """Respeta el robots.txt de cada web."""

    def __init__(self, base):
        self.rp = RobotFileParser()
        self.sitemaps = []
        self.comodines = []
        try:
            texto = get(base + "/robots.txt", intentos=2)
            self.rp.parse(texto.splitlines())
            self.comodines = _reglas_con_comodin(texto)
            self.sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", texto)
            self.estado = "leído"
        except HTTPError as e:
            if e.code in (401, 403):
                # La web no deja leer su robots.txt: por prudencia no se lee nada.
                self.rp.parse(["User-agent: *", "Disallow: /"])
                self.estado = f"bloqueado ({e.code})"
            else:
                self.rp.parse([])
                self.estado = f"no existe ({e.code})"
        except Exception as e:
            self.rp.parse([])
            self.estado = f"no se pudo leer ({type(e).__name__})"

        try:
            self.demora = float(self.rp.crawl_delay(UA) or 0)
        except Exception:
            self.demora = 0

    def permite(self, url):
        if not self.rp.can_fetch(UA, url):
            return False
        partes = urlparse(url)
        ruta = (partes.path or "/") + ("?" + partes.query if partes.query else "")
        return not any(r.match(ruta) for r in self.comodines)


def _reglas_con_comodin(texto):
    """Disallow con * o $ (el lector de robots.txt de Python 3.12 no los entiende): se aplican
    aparte, para el grupo "User-agent: *" o el nuestro. Por prudencia no se miran los Allow."""
    reglas, aplica, en_agentes = [], False, False
    for linea in texto.splitlines():
        linea = linea.split("#", 1)[0].strip()
        if ":" not in linea:
            continue
        clave, valor = (x.strip() for x in linea.split(":", 1))
        clave = clave.lower()
        if clave == "user-agent":
            if not en_agentes:
                aplica = False
            en_agentes = True
            if valor == "*" or (valor and valor.lower() in UA.lower()):
                aplica = True
            continue
        en_agentes = False
        if clave == "disallow" and aplica and valor and ("*" in valor or valor.endswith("$")):
            patron = re.escape(valor.rstrip("$")).replace(r"\*", ".*") + ("$" if valor.endswith("$") else "")
            reglas.append(re.compile(patron))
    return reglas


def esperar(robots):
    """Pausa entre pedidos: la nuestra o la que pide la web en su robots.txt (Crawl-delay), la que sea mayor."""
    time.sleep(max(PAUSA, getattr(robots, "demora", 0) or 0))


# ---------------------------------------------------------------- utilidades

def limpiar_texto(t):
    if not t:
        return ""
    t = html.unescape(str(t))
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_precio(v):
    """Convierte 12800, '12800.00', '$ 12.800,00', '12.800' a float. Devuelve None si no hay precio."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        dec = s.rpartition(",")[2]
        s = s.replace(",", ".") if len(dec) <= 2 and s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3):
        s = s.replace(".", "")
    try:
        p = float(s)
    except ValueError:
        return None
    return p if p > 0 else None


def recortar(texto, largo=300):
    texto = limpiar_texto(texto)
    if len(texto) <= largo:
        return texto
    return texto[:largo].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def item(prov, nombre, precio, url, imagen=None, categoria=None, disponible=True, descripcion=None):
    nombre = limpiar_texto(nombre)
    if not nombre or not url:
        return None
    descripcion = recortar(descripcion)
    if descripcion.lower() == nombre.lower():
        descripcion = ""
    return {
        "n": nombre[:200],
        "p": round(precio, 2) if precio else None,
        "u": url,
        "i": imagen or "",
        "c": limpiar_texto(categoria)[:60],
        "s": prov["id"],
        "d": 1 if disponible else 0,
        "t": descripcion,
    }


def base_de(prov):
    u = urlparse(prov["url"])
    return f"{u.scheme}://{u.netloc}"


# ---------------------------------------------------------------- Shopify

def shopify(prov, maximo, robots):
    base = base_de(prov)
    out = []
    for pagina in range(1, 200):
        datos = json.loads(get(f"{base}/products.json?limit=250&page={pagina}"))
        prods = datos.get("products") or []
        if not prods:
            break
        for p in prods:
            variantes = p.get("variants") or [{}]
            precios = [x for x in (parse_precio(v.get("price")) for v in variantes) if x]
            disponible = any(v.get("available", True) for v in variantes)
            imgs = p.get("images") or [{}]
            it = item(prov, p.get("title"), min(precios) if precios else None,
                      f"{base}/products/{p.get('handle')}", imgs[0].get("src"),
                      p.get("product_type"), disponible, p.get("body_html"))
            if it:
                out.append(it)
        if len(out) >= maximo:
            break
        esperar(robots)
    return out[:maximo]


# ---------------------------------------------------------------- WooCommerce

def woocommerce(prov, maximo, robots):
    """Primero prueba la Store API pública de WooCommerce; si está cerrada, usa sitemap + datos estructurados."""
    base = base_de(prov)
    out = []
    if robots.estado != "leído" and not robots.estado.startswith("no existe") \
            or not robots.permite(f"{base}/wp-json/wc/store/v1/products"):
        print("    robots.txt no permite (o no confirma) la Store API; uso el sitemap", flush=True)
        return sitemap_jsonld(prov, maximo, robots)
    try:
        for pagina in range(1, 500):
            url = f"{base}/wp-json/wc/store/v1/products?per_page=100&page={pagina}"
            texto = get(url)
            datos = json.loads(texto)
            if not isinstance(datos, list) or not datos:
                break
            for p in datos:
                pr = p.get("prices") or {}
                minor = int(pr.get("currency_minor_unit") or 0)
                bruto = pr.get("price")
                precio = (int(bruto) / 10 ** minor) if bruto not in (None, "", "0") else None
                imgs = p.get("images") or [{}]
                cats = p.get("categories") or [{}]
                it = item(prov, p.get("name"), precio, p.get("permalink"),
                          imgs[0].get("src") if imgs else None,
                          cats[0].get("name") if cats else None,
                          p.get("is_in_stock", True),
                          p.get("short_description") or p.get("description"))
                if it:
                    out.append(it)
            if len(out) >= maximo or len(datos) < 100:
                break
            esperar(robots)
        if out:
            return out[:maximo]
    except (HTTPError, ValueError) as e:
        print(f"    Store API no disponible ({e}); pruebo con el sitemap", flush=True)
    return sitemap_jsonld(prov, maximo, robots)


# ---------------------------------------------------------------- Sitemap + datos estructurados
# Sirve para Tiendanube, Jumpseller y muchas webs a medida: casi todas publican
# un sitemap.xml y, en cada producto, un bloque JSON-LD (schema.org/Product)
# o metadatos OpenGraph con el precio.

def urls_por_rastreo(prov, robots, maximo, al_leer=None):
    """Para webs con sitemap viejo o sin sitemap: recorre las categorías y sus páginas
    (según la expresión "seguir") y junta los enlaces de producto (expresión "producto").
    al_leer(url, texto) se llama con cada página de categoría leída."""
    cfg = prov["rastreo"]
    base = base_de(prov)
    re_seguir, re_prod = re.compile(cfg["seguir"]), re.compile(cfg["producto"])
    re_excluir = re.compile(cfg["excluir"]) if cfg.get("excluir") else None
    pendientes = [urljoin(base + "/", u) for u in cfg.get("inicio", ["/"])]
    en_cola, vistas, productos = set(pendientes), set(), {}
    while pendientes and len(vistas) < cfg.get("paginas_max", 3000) and len(productos) < maximo:
        url = pendientes.pop(0)
        vistas.add(url)
        if not robots.permite(url):
            continue
        try:
            texto = get(url, intentos=2)
        except Exception:
            continue
        if al_leer:
            al_leer(url, texto)
        for m in re_prod.finditer(texto):
            enlace = urljoin(url, html.unescape(next(g for g in m.groups() if g)))
            # El mismo producto puede aparecer en varias categorías con otra ruta: se cuenta una vez.
            clave = urlparse(enlace).path.rstrip("/").rsplit("/", 1)[-1]
            if clave not in productos and robots.permite(enlace):
                productos[clave] = enlace
        for href in re.findall(r'href=["\']([^"\'#]+)["\']', texto):
            destino = urljoin(url, html.unescape(href))
            if destino in en_cola or not re_seguir.search(destino) or (re_excluir and re_excluir.search(destino)):
                continue
            en_cola.add(destino)
            pendientes.append(destino)
        esperar(robots)
    print(f"    {len(vistas)} páginas de categorías recorridas", flush=True)
    return list(productos.values())[:maximo]


def rastreo_tarjetas(prov, maximo, robots):
    """Como el rastreo de categorías, pero toma nombre, precio, foto y stock directamente de las
    tarjetas de cada listado, sin abrir cada producto (para catálogos de miles de productos)."""
    cfg = prov["rastreo"]["tarjetas"]
    base = base_de(prov)
    por_clave = {}

    def al_leer(url, texto):
        for t in _tarjetas_listado(texto, cfg):
            if not (t["link"] and t["nombre"]):
                continue
            enlace = urljoin(url, html.unescape(t["link"]))
            clave = urlparse(enlace).path.rstrip("/").rsplit("/", 1)[-1]
            if clave in por_clave:
                continue
            segmentos = [x for x in urlparse(enlace).path.strip("/").split("/") if x]
            categoria = segmentos[-2].replace("-", " ").title() if len(segmentos) >= 2 else ""
            imagen = urljoin(base + "/", html.unescape(t["imagen"])) if t["imagen"] else None
            if imagen and cfg.get("imagen_grande"):
                imagen = re.sub(cfg["imagen_grande"][0], cfg["imagen_grande"][1], imagen)
            agotado = bool(re.search(r"(sin stock|agotad|out of stock)", t["stock"], re.I))
            it = item(prov, t["nombre"], parse_precio(t["precio"]), enlace, imagen, categoria,
                      not agotado, t["descripcion"])
            if it:
                por_clave[clave] = it

    urls_por_rastreo(prov, robots, maximo, al_leer)
    return list(por_clave.values())[:maximo]


def urls_de_productos(prov, robots, maximo):
    if prov.get("rastreo"):
        return urls_por_rastreo(prov, robots, maximo)
    base = base_de(prov)
    patron = prov.get("patron") or PATRONES_PRODUCTO.get(prov["plataforma"])
    pendientes = list(dict.fromkeys(robots.sitemaps + [base + "/sitemap.xml"]))
    vistos, urls = set(), []
    limite = maximo if patron else maximo * 3
    while pendientes and len(urls) < limite and len(vistos) < 60:
        sm = pendientes.pop(0)
        if sm in vistos:
            continue
        vistos.add(sm)
        try:
            xml = get(sm, intentos=2)
        except Exception:
            continue
        locs = [html.unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.S)]
        if "<sitemapindex" in xml:
            # Primero los sitemaps de productos; se saltean los de posts, páginas, autores, etc.
            locs = [u for u in locs if re.search(r"product", u, re.I) or not re.search(
                r"(post|page|author|tag|blog|news|user|taxonom)[-_]?sitemap|sitemap[-_]?(post|page|author|tag|user|taxonom)", u, re.I)]
            locs.sort(key=lambda u: 0 if re.search(r"product", u, re.I) else 1)
            pendientes = locs + pendientes
            continue
        for u in locs:
            if u.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf")):
                continue
            if patron and not re.search(patron, u):
                continue
            urls.append(u)
    urls = [u for u in dict.fromkeys(urls) if robots.permite(u)]
    return urls[:limite]


def _aplanar(d):
    if isinstance(d, list):
        for x in d:
            yield from _aplanar(x)
    elif isinstance(d, dict):
        if "@graph" in d:
            yield from _aplanar(d["@graph"])
        yield d
        # Tiendanube pone el producto principal y las migas dentro de la WebPage.
        for clave in ("mainEntity", "breadcrumb"):
            if isinstance(d.get(clave), (dict, list)):
                yield from _aplanar(d[clave])


def _tipo(d):
    """Devuelve los tipos sin prefijo: 'http://schema.org/Product' -> 'Product'."""
    t = d.get("@type", "")
    tipos = t if isinstance(t, list) else [t]
    return [re.split(r"[/#:]", str(x).rstrip("/"))[-1] for x in tipos]


def _imagen(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl")
    return v if isinstance(v, str) else None


def _precio_oferta(offers):
    """Devuelve (precio, disponible) a partir de 'offers' (dict, lista o AggregateOffer)."""
    ofertas = list(_aplanar(offers)) if offers else []
    precios, disponible = [], None
    for o in ofertas:
        for clave in ("price", "lowPrice"):
            p = parse_precio(o.get(clave))
            if p:
                precios.append(p)
        spec = o.get("priceSpecification")
        for s in _aplanar(spec) if spec else []:
            p = parse_precio(s.get("price"))
            if p:
                precios.append(p)
        av = str(o.get("availability", ""))
        if av:
            en_stock = "InStock" in av or "PreOrder" in av or "LimitedAvailability" in av
            disponible = en_stock if disponible is None else (disponible or en_stock)
    return (min(precios) if precios else None), (True if disponible is None else disponible)


def _meta(texto, prop):
    m = re.search(r'<meta[^>]+(?:property|name|itemprop)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(prop), texto, re.I) \
        or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']%s["\']' % re.escape(prop), texto, re.I)
    return html.unescape(m.group(1)).strip() if m else None


def _producto_de_la_pagina(nodos, texto, url):
    """Elige el Product que corresponde a ESTA página. Muchas webs (Tiendanube, etc.) suman los datos
    de los "productos relacionados" en la misma página, y a veces el producto principal ni siquiera
    está: tomar el primero mezcla nombres y precios. Si no hay uno que sea claramente de esta página,
    devuelve None y se usan los metadatos de la propia página (OpenGraph / tiendanube:price)."""
    ruta = urlparse(url).path.rstrip("/")

    def rutas_declaradas(n):
        vals = [n.get("url"), n.get("@id"), n.get("mainEntityOfPage")]
        ofertas = n.get("offers")
        vals += ofertas if isinstance(ofertas, list) else [ofertas]
        out = set()
        for v in vals:
            if isinstance(v, dict):
                v = v.get("@id") or v.get("url")
            if isinstance(v, str) and v.startswith(("http", "/")):
                out.add(urlparse(v).path.rstrip("/"))
        return out

    candidatos = [n for n in nodos if {"Product", "ProductGroup"} & set(_tipo(n))]
    sin_ruta = []
    for n in candidatos:
        rutas = rutas_declaradas(n)
        if ruta in rutas:
            return n
        if not rutas or rutas == {""}:
            sin_ruta.append(n)  # no dice de qué página es
    titulo = limpiar_texto(_meta(texto, "og:title") or "").lower()
    if titulo:
        for n in candidatos:
            if limpiar_texto(n.get("name")).lower() == titulo:
                return n
    if len(sin_ruta) == 1 and len(candidatos) == 1:
        return sin_ruta[0]
    return None


def _moneda(producto):
    for o in _aplanar(producto.get("offers") or []):
        m = str(o.get("priceCurrency") or "").upper()
        if m:
            return m
    return ""


def _cotizacion_usd(texto):
    """Cotización del dólar que usa la propia web para pasar a pesos (si la publica)."""
    m = re.search(r'data-usd-ars-rate=\\?["\']?([\d.,]+)', texto)
    return parse_precio(m.group(1)) if m else None


def extraer_producto(texto, url, prov):
    """Saca nombre, precio, imagen y categoría de la página de un producto."""
    bloques = re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", texto, re.S | re.I)
    nodos = []
    for b in bloques:
        b = b.strip()
        try:
            nodos.extend(_aplanar(json.loads(b)))
        except ValueError:
            try:
                nodos.extend(_aplanar(json.loads(re.sub(r"[\x00-\x1f]", " ", b))))
            except ValueError:
                continue

    producto = _producto_de_la_pagina(nodos, texto, url)
    categoria = None
    migas = next((n for n in nodos if "BreadcrumbList" in _tipo(n)), None)
    if migas:
        nombres = [limpiar_texto((e.get("item") or {}).get("name") if isinstance(e.get("item"), dict) else e.get("name"))
                   for e in migas.get("itemListElement") or [] if isinstance(e, dict)]
        nombres = [n for n in nombres if n and n.lower() not in ("inicio", "home", "productos", "tienda")]
        if len(nombres) >= 2:
            categoria = nombres[-2]

    if producto:
        precio, disponible = _precio_oferta(producto.get("offers"))
        if precio is None and producto.get("hasVariant"):
            variantes = list(_aplanar(producto["hasVariant"]))
            pares = [_precio_oferta(v.get("offers")) for v in variantes]
            precios = [p for p, _ in pares if p]
            precio = min(precios) if precios else None
            disponible = any(d for _, d in pares) if pares else True
        cat = producto.get("category")
        if isinstance(cat, dict):
            cat = cat.get("name")
        descripcion = producto.get("description") or _meta(texto, "og:description")
        if precio and _moneda(producto) == "USD":
            # Algunas tiendas (p. ej. en Planeta Once) publican en dólares y la web los pasa a pesos
            # con su propia cotización: se usa esa misma, para mostrar lo que ve el cliente.
            cotizacion = _cotizacion_usd(texto)
            if not cotizacion:
                return None  # sin cotización no se puede dar un precio en pesos confiable
            usd = precio
            precio = round(usd * cotizacion, 2)
            nota = f"Precio publicado en dólares: US$ {usd:,.2f} (cotización de la web: ${cotizacion:,.0f})"
            nota = nota.replace(",", "X").replace(".", ",").replace("X", ".")
            descripcion = f"{nota}. {descripcion}" if descripcion else nota
        return item(prov, producto.get("name"), precio, url,
                    _imagen(producto.get("image")), categoria or cat, disponible, descripcion)

    # Sin JSON-LD: metadatos OpenGraph / microdatos (muchas Tiendanube y webs a medida los tienen)
    precio = parse_precio(_meta(texto, "product:price:amount") or _meta(texto, "og:price:amount")
                          or _meta(texto, "tiendanube:price") or _meta(texto, "price"))
    nombre = _meta(texto, "og:title")
    if not (precio and nombre):
        return None
    disp = (_meta(texto, "product:availability") or _meta(texto, "og:availability") or "instock").lower()
    disponible = not any(x in disp for x in ("out", "oos", "agotado", "sin stock"))
    stock = _meta(texto, "tiendanube:stock")
    if stock is not None and re.fullmatch(r"-?\d+", stock.strip()):
        disponible = int(stock) > 0
    return item(prov, nombre, precio, url, _meta(texto, "og:image:secure_url") or _meta(texto, "og:image"), categoria,
                disponible, _meta(texto, "og:description"))


def sitemap_jsonld(prov, maximo, robots):
    urls = urls_de_productos(prov, robots, maximo)
    print(f"    {len(urls)} URLs candidatas en el sitemap", flush=True)
    if not urls:
        return []

    def bajar(u):
        try:
            texto = get(u, intentos=2)
        except Exception:
            return None
        finally:
            esperar(robots)
        return extraer_producto(texto, u, prov)

    out, sin_datos = [], 0
    hilos = 1 if getattr(robots, "demora", 0) else prov.get("hilos", HILOS)
    with ThreadPoolExecutor(hilos) as pool:
        for i, it in enumerate(pool.map(bajar, urls), 1):
            if it:
                out.append(it)
            else:
                sin_datos += 1
            if i % 1000 == 0:
                print(f"    {i} de {len(urls)} páginas leídas ({len(out)} productos)", flush=True)
            # Si muchas páginas seguidas no tienen datos de producto, no tiene sentido seguir.
            if i == LIMITE_SIN_DATOS and not out:
                print(f"    las primeras {LIMITE_SIN_DATOS} páginas no tienen datos de producto; corto", flush=True)
                pool.shutdown(wait=False, cancel_futures=True)
                break
            if len(out) >= maximo:
                pool.shutdown(wait=False, cancel_futures=True)
                break
    return out[:maximo]


# ---------------------------------------------------------------- Catálogos PHP con clave única
# Webs como Oestech y Laxshi: un formulario pide una clave de acceso (la misma para
# todos los clientes) y después muestran bloques "Código: / nombre / Ctdad. por bulto / $precio".
# La clave se lee de una variable de entorno CLAVE_<ID> (en GitHub: Settings → Secrets).

RE_CODIGO = re.compile(r"C[óo]digo:\s*", re.I)
RE_PRECIO = re.compile(r"\$\s*([\d.]+(?:,\d{1,2})?)")
RE_BULTO = re.compile(r"(?:Ctdad\.?|Cantidad)\s*por\s*bulto:\s*(\d+)", re.I)
RE_IMG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def productos_por_codigo(texto_html, url_pagina, prov):
    """Extrae productos de páginas que listan bloques con 'Código:'."""
    inicios = [m.start() for m in RE_CODIGO.finditer(texto_html)]
    out = []
    for k, ini in enumerate(inicios):
        fin = inicios[k + 1] if k + 1 < len(inicios) else min(len(texto_html), ini + 3000)
        previo = texto_html[max(inicios[k - 1] if k else 0, ini - 2500):ini]
        bloque = texto_html[ini:fin]
        lineas = [limpiar_texto(x) for x in re.split(r"<[^>]+>|\n", bloque)]
        lineas = [x for x in lineas if x]
        if not lineas:
            continue
        codigo = RE_CODIGO.sub("", lineas[0]).strip()
        nombre = next((x for x in lineas[1:] if not RE_BULTO.search(x) and not RE_PRECIO.match(x)
                       and not RE_CODIGO.match(x)), None)
        precio = RE_PRECIO.search(" ".join(lineas))
        if not (codigo and nombre and precio):
            continue
        nombre = re.sub(r"(\s*-)+\s*$", "", nombre).strip()
        bulto = RE_BULTO.search(" ".join(lineas))
        imgs = RE_IMG.findall(previo)
        imagen = urljoin(url_pagina, imgs[-1]) if imgs else None
        detalle = f"Código: {codigo}" + (f" · Cantidad por bulto: {bulto.group(1)}" if bulto else "")
        it = item(prov, nombre, parse_precio(precio.group(1)), f"{url_pagina.split('#')[0]}#codigo-{codigo}",
                  imagen, None, True, detalle)
        if it:
            it["_codigo"] = codigo
            out.append(it)
    return out


RE_ANCLA = re.compile(r'<a name="([^"]+)" id="[^"]*">\s*</a>\s*<!--\s*anclaje\s*-->', re.I)


def productos_anclaje(texto_html, url_pagina, prov):
    """Catálogos internos (catalogo2021.php / catalogo2024.php): cada producto empieza con
    <a name="CODIGO"></a> <!-- anclaje --> y sigue con <h1>nombre</h1>, foto, rubro, bulto y precio."""
    marcas = list(RE_ANCLA.finditer(texto_html))
    out = []
    for k, m in enumerate(marcas):
        fin = marcas[k + 1].start() if k + 1 < len(marcas) else len(texto_html)
        b = re.sub(r"<!--.*?-->", "", texto_html[m.end():fin], flags=re.S)  # sin comentarios HTML
        codigo = limpiar_texto(m.group(1))
        nombre = re.search(r"<h1[^>]*>(.*?)</h1>", b, re.S | re.I)
        img = re.search(r'<img[^>]+src="([^"]+)"', b, re.I)
        cat = (re.search(r'class="rubro_centrado"[^>]*>(.*?)</p>', b, re.S | re.I)
               or re.search(r"Rubro:\s*([^<\n]+)", b))
        texto = limpiar_texto(b)
        bulto = re.search(r"(?:Ctdad\.?|Cant\.?)\s*p(?:or|/)\s*bulto:\s*(\d+)", texto, re.I)
        precio = re.search(r"\$\s*([\d.,]+)", texto)
        if not (nombre and precio):
            continue
        detalle = f"Código: {codigo}" + (f" · Cantidad por bulto: {bulto.group(1)}" if bulto else "")
        it = item(prov, re.sub(r"[\s_-]+$", "", limpiar_texto(nombre.group(1))), parse_precio(precio.group(1)),
                  f"{url_pagina.split('#')[0]}#{codigo}", urljoin(url_pagina, img.group(1)) if img else None,
                  limpiar_texto(cat.group(1)).title() if cat else None, True, detalle)
        if it:
            it["_codigo"] = codigo
            out.append(it)
    return out


def clave_php(prov, maximo, robots):
    base = base_de(prov)
    variable = "CLAVE_" + prov["id"].upper()
    clave = os.environ.get(variable)
    sesion = build_opener(HTTPCookieProcessor(CookieJar()))

    def abrir(url, datos=None):
        cuerpo = urlencode(datos).encode() if datos else None
        req = Request(url, data=cuerpo, headers={"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"})
        with sesion.open(req, timeout=TIMEOUT) as r:
            charset = r.headers.get_content_charset() or "latin-1"
            return r.geturl(), r.read().decode(charset, errors="replace")

    por_codigo = {}
    if clave and prov.get("login"):
        url_login = urljoin(base + "/", prov["login"])
        abrir(url_login, {"clave": clave, "enviar": "Ingresar"})
    elif prov.get("login"):
        print(f"    sin clave ({variable} no está cargada): solo se leen los productos públicos", flush=True)

    try:
        abrir(base + "/")  # algunas webs arman la sesión en la portada antes de mostrar el catálogo
    except Exception:
        pass
    pendientes = [urljoin(base + "/", prov.get("catalogo", "")), base + "/"]
    vistas, diagnostico = set(), None
    while pendientes and len(vistas) < 400 and len(por_codigo) < maximo:
        url = pendientes.pop(0)
        if url in vistas or not robots.permite(url):
            continue
        vistas.add(url)
        try:
            url_final, texto = abrir(url)
        except Exception:
            continue
        if diagnostico is None:
            pide_clave = bool(re.search(r'type=["\']password', re.sub(r"<!--.*?-->", "", texto, flags=re.S), re.I))
            diagnostico = f"primera página: {url_final} ({len(texto)} bytes, {'pide clave' if pide_clave else 'sin formulario de clave'})"
        lector = productos_anclaje if RE_ANCLA.search(texto) else productos_por_codigo
        for it in lector(texto, url_final, prov):
            por_codigo.setdefault(it.pop("_codigo"), it)
        # seguir enlaces del catálogo: rubros, categorías y páginas
        for href in re.findall(r'href=["\']([^"\'#]+)["\']', texto):
            destino = urljoin(url_final, html.unescape(href))
            if urlparse(destino).netloc != urlparse(base).netloc:
                continue
            if re.search(r"(salir|logout|contacto|envios|preguntas|testimonios|como-comprar|suscripcion|carrito|pedido)", destino, re.I):
                continue
            if re.search(r"(rubro|rub=|categoria|catalogo|listado|pag(ina)?=|page=|index2|productos)", destino, re.I) and destino not in vistas:
                pendientes.append(destino)
        esperar(robots)
    if not por_codigo:
        print(f"    {diagnostico}", flush=True)
    else:
        print(f"    {len(vistas)} páginas recorridas", flush=True)
    return list(por_codigo.values())[:maximo]


# ---------------------------------------------------------------- Webs que cargan con JavaScript
# Abre la página en un navegador automático (Playwright), baja hasta el final para que
# cargue todo y lee los productos que se ven en pantalla, como lo haría una persona.

JS_TARJETAS = """(tarjetas, sel) => tarjetas.map(c => {
  const q = s => s ? (c.querySelector(s)?.innerText || '').trim() : '';
  const img = sel.imagen ? c.querySelector(sel.imagen) : null;
  const a = [...c.querySelectorAll('a[href]')].find(x => /^https?:/.test(x.href));
  return {nombre: q(sel.nombre), precio: q(sel.precio), categoria: q(sel.categoria),
          codigo: q(sel.codigo), extra: q(sel.extra), imagen: img ? img.src : '', link: a ? a.href : ''};
})"""


def item_de_tarjeta(prov, t):
    imagen = t.get("imagen") or ""
    if "placehold" in imagen or imagen.startswith("data:"):
        imagen = ""
    codigo = re.sub(r"^C[óo]d(igo)?\.?:\s*", "", t.get("codigo") or "", flags=re.I).strip()
    extra = t.get("extra") or ""
    extra = re.sub(r"(?i)cantidad por bulto:", "Cantidad por bulto:", extra.title()) if extra.isupper() else extra
    detalle = " · ".join(x for x in (f"Código: {codigo}" if codigo else "", extra) if x)
    url = t.get("link") or (prov["url"].rstrip("/") + "/#codigo-" + (codigo or re.sub(r"\W+", "-", t.get("nombre", "")).lower()))
    return item(prov, t.get("nombre"), parse_precio(t.get("precio")), url, imagen,
                (t.get("categoria") or "").title(), True, detalle)


def navegador(prov, maximo, robots):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("falta instalar playwright (pip install playwright && python -m playwright install chromium)")
    if not robots.permite(prov["url"]):
        raise RuntimeError("robots.txt no permite leer la página")
    sel = prov["selectores"]
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(user_agent=UA, locale="es-AR", viewport={"width": 1280, "height": 900})
        pg.goto(prov["url"], wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector(sel["tarjeta"], timeout=45000)
        anterior, quietos = -1, 0
        while True:
            n = pg.locator(sel["tarjeta"]).count()
            if n >= maximo:
                break
            quietos = quietos + 1 if n == anterior else 0
            if quietos >= 5:
                break
            anterior = n
            pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            pg.mouse.wheel(0, 4000)
            pg.wait_for_timeout(1500)
        tarjetas = pg.eval_on_selector_all(sel["tarjeta"], JS_TARJETAS, sel)
        b.close()
    print(f"    {len(tarjetas)} tarjetas en pantalla", flush=True)
    return [x for x in (item_de_tarjeta(prov, t) for t in tarjetas) if x][:maximo]


# ---------------------------------------------------------------- Listados HTML paginados
# Para tiendas a medida cuyo listado (/tienda?page=N) ya trae nombre, precio, foto y link.
# La configuración del proveedor indica la URL de las páginas, cómo separar cada tarjeta y
# una expresión regular por dato.

def _tarjetas_listado(texto, cfg):
    partes = texto.split(cfg["tarjeta"])[1:]
    out = []
    for parte in partes:
        datos = {}
        for campo in ("link", "imagen", "nombre", "categoria", "precio", "codigo", "descripcion", "stock"):
            m = re.search(cfg[campo], parte) if cfg.get(campo) else None
            datos[campo] = limpiar_texto(m.group(1)) if m else ""
        out.append(datos)
    return out


def listado_html(prov, maximo, robots):
    """Listados HTML. Dos modos:
    - "url" con {n}: páginas numeradas (/tienda?page=1, 2, ...) hasta que no haya productos nuevos.
    - "categorias": expresión que encuentra en la portada los enlaces a cada categoría; se lee la
      primera página de cada una (para webs cuyo robots.txt no permite paginar)."""
    base = base_de(prov)
    cfg = prov["listado"]
    por_categorias = bool(cfg.get("categorias"))
    if por_categorias:
        portada = get(base + "/")
        paginas = list(dict.fromkeys(urljoin(base + "/", html.unescape(h)) for h in re.findall(cfg["categorias"], portada)))
        print(f"    {len(paginas)} categorías en la portada", flush=True)
    else:
        paginas = (urljoin(base + "/", cfg["url"].format(n=n)) for n in range(1, cfg.get("paginas_max", 300) + 1))
    vistos, out, leidas = set(), [], 0
    for url in paginas:
        if not robots.permite(url):
            if por_categorias:
                continue
            raise RuntimeError(f"robots.txt no permite leer {url}")
        try:
            texto = get(url)
        except Exception:
            if por_categorias:
                continue
            raise
        leidas += 1
        tarjetas = _tarjetas_listado(texto, cfg)
        nuevas = [t for t in tarjetas if t["link"] and t["link"] not in vistos]
        if not nuevas and not por_categorias:  # página vacía o repetida: se terminó el listado
            break
        for t in nuevas:
            vistos.add(t["link"])
            precios = [x for x in (parse_precio(v) for v in re.findall(r"\$?\s*[\d.,]+", t["precio"])) if x]
            partes = []
            if t["codigo"]:
                partes.append(f"Código: {t['codigo']}")
            if len(precios) > 1:
                partes.append("Precios publicados: " + " a ".join(f"${x:,.0f}".replace(",", ".") for x in sorted(precios)))
            if t["descripcion"]:
                partes.append(t["descripcion"])
            it = item(prov, t["nombre"], min(precios) if precios else None, urljoin(base + "/", t["link"]),
                      urljoin(base + "/", t["imagen"]) if t["imagen"] else None, t["categoria"].title(), True,
                      " · ".join(partes))
            if it:
                out.append(it)
        if len(out) >= maximo:
            break
        esperar(robots)
    print(f"    {leidas} páginas del listado", flush=True)
    return out[:maximo]


COLECTORES = {
    "listado_html": listado_html,
    "shopify": shopify,
    "woocommerce": woocommerce,
    "tiendanube": sitemap_jsonld,
    "jumpseller": sitemap_jsonld,
    "a_medida": sitemap_jsonld,
    "rastreo_tarjetas": rastreo_tarjetas,
    "clave_php": clave_php,
    "navegador": navegador,
}


# ---------------------------------------------------------------- principal

def cargar_anterior():
    try:
        return json.loads(ARCHIVO_SALIDA.read_text(encoding="utf-8"))
    except Exception:
        return {"proveedores": [], "productos": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", help="ids de proveedores separados por coma")
    ap.add_argument("--max", type=int, default=MAX_POR_DEFECTO, help="máximo de productos por proveedor")
    args = ap.parse_args()

    proveedores = json.loads(ARCHIVO_PROVEEDORES.read_text(encoding="utf-8"))
    solo = set(args.solo.split(",")) if args.solo else None
    anterior = cargar_anterior()
    estado_anterior = {p["id"]: p for p in anterior.get("proveedores", [])}
    productos_anteriores = {}
    for it in anterior.get("productos", []):
        productos_anteriores.setdefault(it["s"], []).append(it)

    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    estados, productos = [], []

    for prov in proveedores:
        pid = prov["id"]
        previo = estado_anterior.get(pid, {})
        estado = {k: prov.get(k) for k in ("id", "nombre", "url", "plataforma", "nota")}
        conservar = (solo and pid not in solo) or not prov.get("activo", True)

        if conservar:
            productos += productos_anteriores.get(pid, [])
            estado.update({k: previo.get(k) for k in ("estado", "cantidad", "actualizado", "error")})
            if not prov.get("activo", True):
                estado.update(estado="inactivo", cantidad=0)
            estados.append(estado)
            continue

        colector = COLECTORES.get(prov["plataforma"])
        print(f"→ {prov['nombre']} ({prov['plataforma']})", flush=True)
        t0 = time.time()
        try:
            robots = Robots(base_de(prov))
            print(f"    robots.txt: {robots.estado}", flush=True)
            nuevos = colector(prov, prov.get("max", args.max), robots) if colector else []
            vistos, unicos = set(), []
            for it in nuevos:
                if it and it["u"] not in vistos:
                    vistos.add(it["u"])
                    unicos.append(it)
            if not unicos:
                raise RuntimeError("no se encontraron productos")
            productos += unicos
            estado.update(estado="ok", cantidad=len(unicos), actualizado=ahora, error=None)
            print(f"    ✓ {len(unicos)} productos en {time.time() - t0:.0f}s", flush=True)
        except Exception as e:
            # Si falla, se conservan los productos de la última vez que funcionó.
            viejos = productos_anteriores.get(pid, [])
            productos += viejos
            estado.update(estado="error", cantidad=len(viejos), actualizado=previo.get("actualizado"),
                          error=f"{type(e).__name__}: {e}"[:200])
            print(f"    ✗ {estado['error']} (se conservan {len(viejos)} productos anteriores)", flush=True)
        estados.append(estado)

    salida = {"generado": ahora, "proveedores": estados, "productos": productos}
    ARCHIVO_SALIDA.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVO_SALIDA.write_text(json.dumps(salida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ok = sum(1 for e in estados if e.get("estado") == "ok")
    print(f"\nListo: {len(productos)} productos de {ok} proveedores → {ARCHIVO_SALIDA.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
