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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO_PROVEEDORES = RAIZ / "proveedores.json"
ARCHIVO_SALIDA = RAIZ / "docs" / "productos.json"

UA = "Mozilla/5.0 (compatible; BuscadorOnce/1.0; +https://github.com)"
TIMEOUT = 25
HILOS = 4            # pedidos simultáneos por proveedor (moderado, para no cargar las webs)
PAUSA = 0.25         # segundos entre pedidos de cada hilo
MAX_POR_DEFECTO = 5000

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
        try:
            texto = get(base + "/robots.txt", intentos=1)
            self.rp.parse(texto.splitlines())
            self.sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", texto)
        except Exception:
            self.rp.parse([])

    def permite(self, url):
        return self.rp.can_fetch(UA, url)


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
        time.sleep(PAUSA)
    return out[:maximo]


# ---------------------------------------------------------------- WooCommerce

def woocommerce(prov, maximo, robots):
    """Primero prueba la Store API pública de WooCommerce; si está cerrada, usa sitemap + datos estructurados."""
    base = base_de(prov)
    out = []
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
            time.sleep(PAUSA)
        if out:
            return out[:maximo]
    except (HTTPError, ValueError) as e:
        print(f"    Store API no disponible ({e}); pruebo con el sitemap", flush=True)
    return sitemap_jsonld(prov, maximo, robots)


# ---------------------------------------------------------------- Sitemap + datos estructurados
# Sirve para Tiendanube, Jumpseller y muchas webs a medida: casi todas publican
# un sitemap.xml y, en cada producto, un bloque JSON-LD (schema.org/Product)
# o metadatos OpenGraph con el precio.

def urls_de_productos(prov, robots, maximo):
    base = base_de(prov)
    patron = PATRONES_PRODUCTO.get(prov["plataforma"])
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
            locs = [u for u in locs if not re.search(r"(post|page|author|tag|blog|news)[-_]?sitemap|sitemap[-_]?(post|page|author|tag)", u, re.I)]
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


def _tipo(d):
    t = d.get("@type", "")
    return t if isinstance(t, list) else [t]


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

    producto = next((n for n in nodos if {"Product", "ProductGroup"} & set(_tipo(n))), None)
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
        return item(prov, producto.get("name"), precio, url,
                    _imagen(producto.get("image")), categoria or cat, disponible,
                    producto.get("description") or _meta(texto, "og:description"))

    # Sin JSON-LD: metadatos OpenGraph / microdatos (muchas Tiendanube y webs a medida los tienen)
    precio = parse_precio(_meta(texto, "product:price:amount") or _meta(texto, "og:price:amount") or _meta(texto, "price"))
    nombre = _meta(texto, "og:title")
    if not (precio and nombre):
        return None
    disp = (_meta(texto, "product:availability") or _meta(texto, "og:availability") or "instock").lower()
    return item(prov, nombre, precio, url, _meta(texto, "og:image"), categoria,
                not any(x in disp for x in ("out", "oos", "agotado", "sin stock")),
                _meta(texto, "og:description"))


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
            time.sleep(PAUSA)
        return extraer_producto(texto, u, prov)

    out, sin_datos = [], 0
    with ThreadPoolExecutor(HILOS) as pool:
        for i, it in enumerate(pool.map(bajar, urls), 1):
            if it:
                out.append(it)
            else:
                sin_datos += 1
            # Si las primeras 40 páginas no tienen datos de producto, no tiene sentido seguir.
            if i == 40 and not out:
                print("    las primeras 40 páginas no tienen datos de producto; corto", flush=True)
                pool.shutdown(wait=False, cancel_futures=True)
                break
            if len(out) >= maximo:
                pool.shutdown(wait=False, cancel_futures=True)
                break
    return out[:maximo]


COLECTORES = {
    "shopify": shopify,
    "woocommerce": woocommerce,
    "tiendanube": sitemap_jsonld,
    "jumpseller": sitemap_jsonld,
    "a_medida": sitemap_jsonld,
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
            nuevos = colector(prov, args.max, robots) if colector else []
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
