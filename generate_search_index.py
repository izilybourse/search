#!/usr/bin/env python3
"""
generate_search_index.py : génère ou met à jour search-index.json pour izilyfire.fr

Python 3.8+, aucune dépendance externe.

Exemples :
  # Scanner un dossier de fichiers HTML locaux
  python generate_search_index.py --src ./site --out ./search-index.json

  # Scanner le site en ligne via son sitemap (utile avec Hostinger Website Builder)
  python generate_search_index.py --sitemap https://izilyfire.fr/sitemap.xml --out ./search-index.json

  # Aperçu sans écrire, en excluant des pages
  python generate_search_index.py --src ./site --exclude "/mentions-legales*" --dry-run

Sans hébergement de fichier JSON (cas Hostinger Website Builder) : ajoutez --js-out pour produire un
bloc <script> contenant l'index, à coller dans un second « Code HTML personnalisé » :
  python generate_search_index.py --sitemap https://izilyfire.fr/sitemap.xml --js-out search-index-inline.html

Mise à jour : si le fichier de sortie existe déjà, il est fusionné par URL.
  - titre et description sont rafraîchis depuis la page ;
  - mots-clés : anciens conservés, nouveaux ajoutés ;
  - catégorie, id et champs personnalisés (ex. "featured") sont conservés ;
  - une entrée avec "manual": true n'est jamais modifiée ;
  - les entrées dont la page n'est plus trouvée sont conservées (sauf --prune).

Métadonnées facultatives à placer dans le <head> des pages :
  <meta name="izs:category" content="Outils">
  <meta name="izs:keywords" content="SCPI, nue-propriété, simulateur">
  <meta name="izs:exclude" content="1">      (page ignorée)
  <meta name="robots" content="noindex">       (page ignorée)
"""

import argparse
import fnmatch
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# ---------------------------------------------------------------- Réglages ---
# Premier segment de l'URL -> catégorie (à adapter à votre arborescence)
CATEGORY_MAP = {
    "blog": "Articles",
    "articles": "Articles",
    "outils": "Outils",
    "simulateurs": "Outils",
    "guides": "Guides",
    "strategies": "Guides",
    "glossaire": "Glossaire",
}
DEFAULT_CATEGORY = "Pages"
MAX_DESCRIPTION = 180
MAX_KEYWORDS = 20
SITE_NAME_PATTERN = re.compile(r"\s*[\|\-–—:]\s*izilyfire(\.fr)?\s*$", re.IGNORECASE)

STOPWORDS = set("""
afin ainsi alors apres après assez aucun aussi autre autres avant avec avez avoir avons beaucoup bien celle celles
celui cependant certains cette ceux chaque comme comment contre dans depuis donc dont encore entre est-ce etait
était etant étant faire fait faut leur leurs lorsque maintenant mais meme même moins notre nous parce peut peuvent
plus plusieurs pour pourquoi quand quelle quelles quels sans selon sera seront serait seulement sont sous souvent
tant tous tout toute toutes tres très votre vous
""".split())


# ------------------------------------------------------------------ Parsing ---
def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


class PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    CAPTURE = {"title", "h1", "h2", "h3", "p"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.canonical = None
        self.titles = []
        self.headings = {"h1": [], "h2": [], "h3": []}
        self.paragraphs = []
        self.body_text = []
        self._skip = 0
        self._cur = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and "content" in a:
                self.meta[key] = a["content"].strip()
        elif tag == "link":
            if "canonical" in a.get("rel", "").lower():
                self.canonical = a.get("href")
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.CAPTURE and self._cur is None:
            self._cur = tag
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == self._cur:
            text = clean("".join(self._buf))
            if text:
                if tag == "title":
                    self.titles.append(text)
                elif tag == "p":
                    self.paragraphs.append(text)
                else:
                    self.headings[tag].append(text)
            self._cur = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._cur:
            self._buf.append(data)
        if self._cur != "title":
            self.body_text.append(data)


def truncate(text, limit):
    text = clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-–—")
    return cut + "…"


def dedupe(values):
    seen, out = set(), []
    for v in values:
        v = clean(v)
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def slugify(text):
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "accueil"


def category_for(url_path):
    parts = [p for p in url_path.split("/") if p]
    if len(parts) > 1:
        return CATEGORY_MAP.get(parts[0].lower(), DEFAULT_CATEGORY)
    return DEFAULT_CATEGORY


def frequent_words(text, n=6):
    words = re.findall(r"[a-zàâäçéèêëîïôöùûüÿœ]{5,}", text.lower())
    counts = Counter(w for w in words if w not in STOPWORDS)
    return [w for w, c in counts.most_common(n) if c >= 3]


def parse_page(html, url_path):
    """Retourne une entrée d'index, ou None si la page doit être ignorée."""
    p = PageParser()
    p.feed(html)
    meta = p.meta

    if "noindex" in meta.get("robots", "").lower() or meta.get("izs:exclude") in ("1", "true", "yes"):
        return None

    title = meta.get("og:title") or (p.titles[0] if p.titles else "") or (p.headings["h1"][0] if p.headings["h1"] else "")
    title = clean(SITE_NAME_PATTERN.sub("", title))
    if not title:
        return None

    description = meta.get("description") or meta.get("og:description") or ""
    if not description:
        description = next((x for x in p.paragraphs if len(x) >= 40), "")
    description = truncate(description, MAX_DESCRIPTION)

    keywords = []
    keywords += [k for k in meta.get("keywords", "").split(",")]
    keywords += [k for k in meta.get("izs:keywords", "").split(",")]
    keywords += [h for h in p.headings["h2"] + p.headings["h3"] if len(h.split()) <= 5][:8]
    keywords += frequent_words(" ".join(p.body_text))
    keywords = dedupe(keywords)[:MAX_KEYWORDS]

    category = clean(meta.get("izs:category", "")) or category_for(url_path)

    return {
        "id": slugify(url_path),
        "title": title,
        "url": url_path,
        "category": category,
        "description": description,
        "keywords": keywords,
    }


# ------------------------------------------------------------------ Sources ---
def excluded(url_path, patterns):
    return any(fnmatch.fnmatch(url_path, pat) for pat in patterns)


def scan_directory(src, patterns, keep_extension):
    src = Path(src).resolve()
    entries = []
    for path in sorted(list(src.rglob("*.html")) + list(src.rglob("*.htm"))):
        rel = path.relative_to(src).as_posix()
        if path.name.lower() in ("404.html", "500.html"):
            continue
        url = "/" + rel
        if path.stem.lower() == "index":
            parent = Path(rel).parent.as_posix()
            url = "/" if parent == "." else "/" + parent
        elif not keep_extension:
            url = re.sub(r"\.html?$", "", url)
        if excluded(url, patterns):
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        entry = parse_page(html, url)
        if entry:
            entries.append(entry)
        else:
            print(f"  ignoré : {rel}")
    return entries


def fetch(url, timeout=20):
    req = Request(url, headers={"User-Agent": "izilyfire-search-indexer/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def sitemap_urls(url, depth=0):
    root = ET.fromstring(fetch(url))
    locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
    if root.tag.endswith("sitemapindex") and depth < 2:
        flat = []
        for sub in locs:
            flat += sitemap_urls(sub, depth + 1)
        return flat
    return locs


def scan_sitemap(sitemap, patterns):
    entries = []
    for page_url in sitemap_urls(sitemap):
        url_path = urlparse(page_url).path or "/"
        if url_path != "/":
            url_path = url_path.rstrip("/")
        if excluded(url_path, patterns):
            continue
        try:
            entry = parse_page(fetch(page_url), url_path)
        except Exception as exc:  # noqa: BLE001
            print(f"  erreur {page_url} : {exc}")
            continue
        if entry:
            entries.append(entry)
        else:
            print(f"  ignoré : {url_path}")
    return entries


# ------------------------------------------------------------------- Fusion ---
def merge(existing, scanned, prune):
    old_by_url = {e.get("url"): e for e in existing}
    merged = []
    for new in scanned:
        old = old_by_url.pop(new["url"], None)
        if not old:
            merged.append(new)
        elif old.get("manual"):
            merged.append(old)
        else:
            entry = dict(old)
            entry["title"] = new["title"]
            entry["description"] = new["description"] or old.get("description", "")
            entry["keywords"] = dedupe(list(old.get("keywords", [])) + new["keywords"])[:MAX_KEYWORDS]
            entry.setdefault("category", new["category"])
            entry.setdefault("id", new["id"])
            merged.append(entry)
    if not prune:
        merged += list(old_by_url.values())
    return merged


def inline_block(entries):
    """Index sous forme de <script> autonome (lu par le module de recherche via window.IZS_INDEX)."""
    data = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return ("<!-- izilyfire.fr : index de recherche (généré par generate_search_index.py) -->\n"
            "<script>window.IZS_INDEX = " + data + ";</script>\n")


# --------------------------------------------------------------------- Main ---
def main():
    ap = argparse.ArgumentParser(description="Génère ou met à jour search-index.json")
    ap.add_argument("--src", help="dossier contenant les fichiers HTML locaux")
    ap.add_argument("--sitemap", help="URL du sitemap.xml du site en ligne")
    ap.add_argument("--out", default="search-index.json", help="fichier de sortie (défaut : search-index.json)")
    ap.add_argument("--js-out", metavar="FICHIER",
                    help="écrit aussi l’index sous forme de bloc <script> prêt à coller (sans hébergement JSON)")
    ap.add_argument("--exclude", action="append", default=[], metavar="MOTIF",
                    help='motif d’URL à ignorer, ex. "/mentions-legales*" (répétable)')
    ap.add_argument("--keep-extension", action="store_true", help="garder .html dans les URL (mode --src)")
    ap.add_argument("--prune", action="store_true", help="supprimer les entrées dont la page n’est plus trouvée")
    ap.add_argument("--fresh", action="store_true", help="ignorer l’index existant et repartir de zéro")
    ap.add_argument("--dry-run", action="store_true", help="afficher le résultat sans écrire le fichier")
    args = ap.parse_args()

    if not args.src and not args.sitemap:
        ap.error("indiquez --src (dossier local) et/ou --sitemap (URL)")

    scanned = []
    if args.src:
        print(f"Analyse du dossier {args.src} …")
        scanned += scan_directory(args.src, args.exclude, args.keep_extension)
    if args.sitemap:
        print(f"Analyse du sitemap {args.sitemap} …")
        scanned += scan_sitemap(args.sitemap, args.exclude)

    unique = {}
    for e in scanned:
        unique.setdefault(e["url"], e)
    scanned = list(unique.values())

    out_path = Path(args.out)
    existing = []
    if out_path.exists() and not args.fresh:
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            existing = data if isinstance(data, list) else data.get("items", [])
        except json.JSONDecodeError as exc:
            sys.exit(f"Index existant illisible ({exc}). Corrigez-le ou relancez avec --fresh.")

    result = merge(existing, scanned, args.prune)
    result.sort(key=lambda e: (e.get("category", ""), e.get("title", "").lower()))
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"

    if args.dry_run:
        print(payload)
    else:
        out_path.write_text(payload, encoding="utf-8")
        if args.js_out:
            Path(args.js_out).write_text(inline_block(result), encoding="utf-8")
            print(f"Bloc à coller dans Hostinger → {args.js_out}")
    print(f"{len(scanned)} page(s) analysée(s), {len(result)} entrée(s) dans l’index"
          + ("" if args.dry_run else f" → {out_path}"))


if __name__ == "__main__":
    main()
