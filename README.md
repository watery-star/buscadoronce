# Buscador Once

Busca productos y precios de los proveedores mayoristas de Once en un solo lugar.

- `proveedores.json`: la lista de proveedores (nombre, web, plataforma, activo sí/no).
- `colector/colectar.py`: trae el catálogo de cada web y lo guarda en `docs/productos.json`.
- `docs/index.html`: la página del buscador (se publica con GitHub Pages).
- `.github/workflows/actualizar.yml`: corre el colector todos los días a las 6:17 (hora de Argentina).

## Puesta en marcha (una sola vez)

1. Creá un repositorio en GitHub llamado `buscador-once` (puede ser público o privado; Pages gratis requiere público).
2. Subí todos los archivos de esta carpeta, incluida la carpeta oculta `.github`.
   Si subís desde la web y la carpeta `.github` no aparece, creala a mano:
   *Add file → Create new file*, nombre `.github/workflows/actualizar.yml`, y pegá el contenido del archivo.
3. **Settings → Pages**: en *Source* elegí *Deploy from a branch*, rama `main`, carpeta `/docs`. Guardá.
4. **Settings → Actions → General**: en *Workflow permissions* elegí *Read and write permissions*. Guardá.
5. **Actions → Actualizar productos → Run workflow** para la primera carga (tarda entre 20 y 60 minutos).

El buscador queda en `https://TU-USUARIO.github.io/buscador-once/`.

## Agregar o sacar un proveedor

Editá `proveedores.json`. Plataformas posibles: `tiendanube`, `woocommerce`, `shopify`, `jumpseller`, `a_medida`.
Con `"activo": false` el proveedor queda en la lista pero no se consulta.

## Si un proveedor falla

En el buscador, el link **ver estado** muestra qué proveedores se actualizaron y el error de los que fallaron.
Si una web falla, se conservan los productos de la última vez que funcionó.
Para reintentar solo uno: *Run workflow* y escribí su id (por ejemplo `mzstore`).

## Cómo funciona el colector

- **Shopify**: lee `/products.json`, que Shopify publica en todas las tiendas.
- **WooCommerce**: usa la API pública de la tienda (`/wp-json/wc/store/v1/products`); si está cerrada, usa el método siguiente.
- **Tiendanube, Jumpseller y webs a medida**: recorre el `sitemap.xml` y lee de cada producto los datos estructurados
  (schema.org) que la web publica para Google.

Respeta el `robots.txt` de cada web y hace pocos pedidos a la vez.

## Probar en tu computadora

```
python3 -m unittest colector/test_colectar.py
python3 colector/colectar.py --solo mzstore --max 50
cd docs && python3 -m http.server 8000   # abrí http://localhost:8000
```
