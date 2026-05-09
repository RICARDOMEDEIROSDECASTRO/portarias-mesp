#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza o site estático de Portarias MESP usando a lógica de consulta do Ro-DOU.

A ideia é reproduzir, em GitHub Actions, a pesquisa que o Ro-DOU faz sobre a
busca oficial do DOU/Imprensa Nacional: termo, seção, data e filtros de ato.

Critério de inclusão na base:
    - o TÍTULO da publicação precisa conter PORTARIA e MESP;
    - exemplos aceitos: PORTARIA MESP Nº 77, PORTARIA MEsp Nº 77,
      PORTARIA MESP/GM Nº 77;
    - menções soltas no corpo do texto, como "considerando a Portaria MESP...",
      não entram se o título do ato não for Portaria MESP.

Uso:
    python scripts/atualizar_dou.py --dias 15
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.in.gov.br"
SEARCH_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; mesp-portarias-rodou/2.0; +https://www.gov.br/esporte)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Mantemos o foco estrito em Portaria MESP.
TERMOS_RODOU = [
    '"PORTARIA MESP"',
    '"PORTARIA MEsp"',
    'PORTARIA MESP',
]

# Valores usados pela busca do DOU para as seções. O site aceita variações;
# por isso o script consulta tanto "todos" quanto seções individualizadas.
SECOES = ["", "do1", "do2", "do3", "doe", "edicao_extra", "edicao_suplementar"]


@dataclass
class Registro:
    data: str
    ano: int
    mes: int
    secao: str
    tipo: str
    titulo: str
    numero: str
    orgao: str
    resumo: str
    link: str


def sem_acentos(s: str) -> str:
    s = str(s or "")
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: str) -> str:
    s = html.unescape(str(s or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    s = sem_acentos(s).upper()
    s = s.replace("º", " ").replace("°", " ")
    s = re.sub(r"[^A-Z0-9/.-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def eh_portaria_mesp_titulo(titulo: str) -> bool:
    t = norm(titulo)
    return "PORTARIA" in t and "MESP" in t


def parse_data_br(s: str) -> date | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def data_key(reg: dict) -> int:
    dt = parse_data_br(reg.get("data", ""))
    if not dt:
        return 0
    return dt.year * 10000 + dt.month * 100 + dt.day


def numero_key(reg: dict) -> int:
    n = re.sub(r"\D", "", str(reg.get("numero", "")))
    return int(n or 0)


def numero_portaria(titulo: str) -> str:
    t = norm(titulo)
    # PORTARIA MESP Nº 77; PORTARIA MESP/GM Nº 77; PORTARIA MEsp N. 77
    m = re.search(r"PORTARIA\s+MESP(?:/[A-Z0-9]+)?\s*(?:N|NO|Nº|N°|NUMERO|N\.)?\s*([0-9]+(?:[./-][0-9]+)*)", t)
    if m:
        return m.group(1)
    m = re.search(r"PORTARIA.*?MESP.*?([0-9]+(?:[./-][0-9]+)*)", t)
    return m.group(1) if m else ""


def extrair_const_data(index_html: str) -> list[dict]:
    m = re.search(r"const\s+DATA\s*=\s*(\[.*?\]);\s*\nconst\s+months\s*=", index_html, flags=re.S)
    if not m:
        raise RuntimeError("Não encontrei o bloco `const DATA = [...]` no index.html.")
    return json.loads(m.group(1))


def substituir_const_data(index_html: str, data: list[dict]) -> str:
    novo_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return re.sub(
        r"const\s+DATA\s*=\s*\[.*?\];\s*\nconst\s+months\s*=",
        "const DATA = " + novo_json + ";\nconst months=",
        index_html,
        flags=re.S,
    )


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def extrair_links_do_html(html_text: str) -> set[str]:
    texto = html.unescape(html_text)
    links: set[str] = set()

    # Links em atributos HTML normais.
    soup = BeautifulSoup(texto, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/web/dou/-/" in href:
            links.add(urljoin(BASE_URL, href.split("?")[0]))

    # Links dentro de JSON/scripts escapados.
    for raw in re.findall(r"https?://(?:www\.)?in\.gov\.br/(?:en/)?web/dou/-/[^\"'<>\s\\]+", texto):
        links.add(unquote(raw).split("?")[0])
    for raw in re.findall(r"/(?:en/)?web/dou/-/[^\"'<>\s\\]+", texto):
        links.add(urljoin(BASE_URL, unquote(raw)).split("?")[0])

    # Normaliza /en/web/dou para /web/dou.
    normalizados = set()
    for link in links:
        normalizados.add(link.replace("https://in.gov.br/en/web/dou/-/", "https://www.in.gov.br/web/dou/-/")
                          .replace("https://www.in.gov.br/en/web/dou/-/", "https://www.in.gov.br/web/dou/-/"))
    return normalizados


def buscar_links_rodou(sess: requests.Session, dia: date, termo: str, secao: str) -> set[str]:
    """Consulta a busca oficial do DOU com parâmetros equivalentes aos do Ro-DOU.

    O Ro-DOU automatiza a busca da Imprensa Nacional. Aqui fazemos o mesmo,
    mas sem Airflow, para funcionar dentro do GitHub Actions.
    """
    datas = [dia.strftime("%d-%m-%Y"), dia.strftime("%d/%m/%Y")]
    links: set[str] = set()

    for data_str in datas:
        param_variants = []

        # Variante clássica usada pelo buscador do DOU.
        p = {"q": termo, "exactDate": data_str, "sortType": "0"}
        if secao:
            p["s"] = secao
        param_variants.append(p)

        # Variante com intervalo explícito, útil quando exactDate falha.
        p = {"q": termo, "publishFrom": data_str, "publishTo": data_str, "sortType": "0"}
        if secao:
            p["s"] = secao
        param_variants.append(p)

        # Variante aproximada ao filtro avançado: título + tipo de ato.
        p = {
            "q": termo,
            "exactDate": data_str,
            "sortType": "0",
            "field": "TITULO",
            "pubtype": "Portaria",
        }
        if secao:
            p["s"] = secao
        param_variants.append(p)

        for params in param_variants:
            try:
                r = sess.get(SEARCH_URL, params=params, timeout=60)
                r.raise_for_status()
                encontrados = extrair_links_do_html(r.text)
                links.update(encontrados)
            except Exception as exc:
                print(f"[aviso] falha na busca DOU {dia} {secao or 'todas'} {termo}: {exc}", file=sys.stderr)

    return links


def texto_limpo(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> str:
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            txt = strip_html(node.get_text(" "))
            if txt:
                return txt
    return ""


def secao_from_text(texto: str, secao_hint: str) -> str:
    m = re.search(r"Se[cç][aã]o:\s*([123])", texto, flags=re.I)
    if m:
        return "DO" + m.group(1)
    h = (secao_hint or "").lower()
    if "do1" in h:
        return "DO1"
    if "do2" in h:
        return "DO2"
    if "do3" in h:
        return "DO3"
    if "extra" in h:
        return "Extra"
    if "suplementar" in h:
        return "Suplementar"
    return "DOU"


def data_from_text(texto: str, fallback: date) -> date:
    m = re.search(r"Publicado em:\s*(\d{2}/\d{2}/\d{4})", texto, flags=re.I)
    if m:
        dt = parse_data_br(m.group(1))
        if dt:
            return dt
    return fallback


def artigo_para_registro(sess: requests.Session, url: str, dia: date, secao_hint: str) -> Registro | None:
    r = sess.get(url, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    titulo = first_text(soup, ["h1", ".title", ".titulo", "#title"])
    if not titulo:
        meta = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "title"})
        titulo = strip_html(meta.get("content", "")) if meta else ""

    if not eh_portaria_mesp_titulo(titulo):
        return None

    full_text = texto_limpo(soup)

    orgao = first_text(soup, [".orgao-dou-data", ".orgao", ".documento-orgao", ".nome-orgao"])
    if not orgao:
        m = re.search(r"(Minist[eé]rio do Esporte(?:/[A-Za-zÀ-ÿ0-9 .ºª()\-]+){0,4})", full_text, flags=re.I)
        orgao = strip_html(m.group(1)) if m else "Ministério do Esporte"

    pub_date = data_from_text(full_text, dia)
    secao = secao_from_text(full_text, secao_hint)

    resumo = strip_html(full_text)
    # Se possível, corta a partir do título.
    pos = norm(resumo).find(norm(titulo)[:40]) if titulo else -1
    if pos > 0:
        resumo = resumo[pos:]
    resumo = resumo[:420]
    if len(resumo) >= 420:
        resumo += "..."

    return Registro(
        data=pub_date.strftime("%d/%m/%Y"),
        ano=pub_date.year,
        mes=pub_date.month,
        secao=secao,
        tipo="Portaria",
        titulo=strip_html(titulo),
        numero=numero_portaria(titulo),
        orgao=strip_html(orgao),
        resumo=resumo,
        link=url,
    )


def atualizar(index_path: Path, dias: int, pausa: float) -> int:
    html_atual = index_path.read_text(encoding="utf-8")
    dados = extrair_const_data(html_atual)

    links_existentes = {str(r.get("link", "")).strip() for r in dados if r.get("link")}
    chaves_existentes = {
        (norm(r.get("titulo", "")), str(r.get("data", "")))
        for r in dados
    }

    novos: list[dict] = []
    sess = session()
    hoje = date.today()
    datas = [hoje - timedelta(days=i) for i in range(dias)]

    for dia in datas:
        candidatos: set[tuple[str, str]] = set()
        for termo in TERMOS_RODOU:
            for secao in SECOES:
                links = buscar_links_rodou(sess, dia, termo, secao)
                for link in links:
                    candidatos.add((link, secao))
        print(f"{dia}: {len(candidatos)} link(s) candidato(s) Ro-DOU/DOU")

        for link, secao in sorted(candidatos):
            if link in links_existentes:
                continue
            try:
                reg = artigo_para_registro(sess, link, dia, secao)
                time.sleep(pausa)
            except Exception as exc:
                print(f"[aviso] falha ao ler {link}: {exc}", file=sys.stderr)
                continue
            if not reg:
                continue
            d = asdict(reg)
            chave = (norm(d.get("titulo", "")), str(d.get("data", "")))
            if chave in chaves_existentes:
                continue
            novos.append(d)
            links_existentes.add(link)
            chaves_existentes.add(chave)
            print(f"+ {d['data']} | {d['titulo']} | {d['link']}")

    if not novos:
        print("Nenhuma Portaria MESP nova encontrada no período pesquisado.")
        return 0

    dados_atualizados = dados + novos

    # Deduplicação final por link e, subsidiariamente, por título/data.
    vistos_links: set[str] = set()
    vistos_chaves: set[tuple[str, str]] = set()
    dedup: list[dict] = []
    for r in dados_atualizados:
        link = str(r.get("link", "")).strip()
        chave = (norm(r.get("titulo", "")), str(r.get("data", "")))
        if link and link in vistos_links:
            continue
        if chave in vistos_chaves:
            continue
        if link:
            vistos_links.add(link)
        vistos_chaves.add(chave)
        dedup.append(r)

    dedup.sort(key=lambda r: (data_key(r), numero_key(r)), reverse=True)
    novo_html = substituir_const_data(html_atual, dedup)
    index_path.write_text(novo_html, encoding="utf-8")
    print(f"Atualização concluída: {len(novos)} novo(s) registro(s). Total: {len(dedup)}.")
    return len(novos)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index.html", help="Caminho para o index.html")
    ap.add_argument("--dias", type=int, default=15, help="Quantidade de dias recentes a consultar")
    ap.add_argument("--pausa", type=float, default=0.6, help="Pausa entre leituras de páginas")
    args = ap.parse_args()
    atualizar(Path(args.index), args.dias, args.pausa)


if __name__ == "__main__":
    main()
