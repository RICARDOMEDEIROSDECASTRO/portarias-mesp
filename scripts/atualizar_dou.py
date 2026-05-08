#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza o site estático de portarias do MESP com novas menções encontradas no DOU.

A rotina foi desenhada para rodar diariamente no GitHub Actions. Ela:
1. lê o `index.html`;
2. extrai o vetor JavaScript `const DATA = [...]`;
3. consulta a busca pública do DOU nos últimos N dias;
4. baixa as páginas das publicações encontradas;
5. mantém apenas publicações cujo título pareça ser Portaria MESP e cujo órgão seja do Ministério do Esporte;
6. junta apenas registros novos, usando o link como chave principal;
7. regrava o `index.html` com a base ordenada por data decrescente.

Uso local:
    python scripts/atualizar_dou.py --dias 3
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.in.gov.br"
SEARCH_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
DEFAULT_TERMO = '"portaria mesp"'
SECOES = ["do1", "do2", "do3"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MESP-Portarias-Bot/1.0; +https://www.gov.br/esporte)",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


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


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


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

def eh_portaria_mesp(texto):
    texto = normalizar(texto)
    return "PORTARIA" in texto and "MESP" in texto

def numero_portaria(titulo: str) -> str:
    t = strip_html(titulo).upper()
    # Exemplos: PORTARIA MESP Nº 46; PORTARIA MESP/SE Nº 72; PORTARIA Nº 10
    m = re.search(r"PORTARIA(?:\s+MESP(?:/[A-Z]+)?|\s+MESP)?\s*(?:N[º°O\.]*)?\s*([0-9]+(?:[./-][0-9]+)*)", t)
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


def buscar_links(sess: requests.Session, dia: date, termo: str, secao: str) -> set[str]:
    """Consulta a busca pública do DOU e extrai links de publicações.

    A página do DOU pode mudar. Por isso, a extração é deliberadamente ampla:
    qualquer link que contenha `/web/dou/-/` é tratado como candidato.
    """
    params = {
        "q": termo,
        "s": secao,
        "exactDate": dia.strftime("%d-%m-%Y"),
        "sortType": "0",
    }
    r = sess.get(SEARCH_URL, params=params, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/web/dou/-/" in href:
            links.add(urljoin(BASE_URL, href.split("?")[0]))
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


def artigo_para_registro(sess: requests.Session, url: str, dia: date, secao_hint: str) -> Registro | None:
    r = sess.get(url, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    titulo = first_text(soup, ["h1", ".title", ".titulo", "#title"])
    if not titulo:
        meta = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "title"})
        titulo = strip_html(meta.get("content", "")) if meta else ""

    full_text = texto_limpo(soup)

    # Órgão/unidade: tenta seletores comuns; se falhar, usa heurística pelo texto.
    orgao = first_text(soup, [".orgao-dou-data", ".orgao", ".documento-orgao", ".nome-orgao"])
    if not orgao:
        m = re.search(r"(Ministério do Esporte(?:/[A-Za-zÀ-ÿ0-9 .ºª()\-]+){0,4})", full_text)
        orgao = strip_html(m.group(1)) if m else "Ministério do Esporte"

    # Critérios conservadores para não incluir extratos ou publicações apenas mencionando Portaria MESP.
    titulo_norm = strip_html(titulo).upper()
    orgao_norm = strip_html(orgao).upper()
    if "PORTARIA" not in titulo_norm or "MESP" not in titulo_norm:
        return None
    if "MINISTÉRIO DO ESPORTE" not in orgao_norm and "MINISTERIO DO ESPORTE" not in orgao_norm:
        return None

    resumo = full_text
    # Prioriza trecho a partir do título, se possível.
    pos = resumo.upper().find(titulo_norm[:30]) if titulo_norm else -1
    if pos >= 0:
        resumo = resumo[pos:]
    resumo = strip_html(resumo)[:420]
    if len(resumo) == 420:
        resumo += "..."

    return Registro(
        data=dia.strftime("%d/%m/%Y"),
        ano=dia.year,
        mes=dia.month,
        secao=secao_hint.upper().replace("DO", "DO"),
        tipo="Portaria",
        titulo=strip_html(titulo),
        numero=numero_portaria(titulo),
        orgao=strip_html(orgao),
        resumo=resumo,
        link=url,
    )


def atualizar(index_path: Path, dias: int, termo: str, pausa: float) -> int:
    html_atual = index_path.read_text(encoding="utf-8")
    dados = extrair_const_data(html_atual)
    links_existentes = {str(r.get("link", "")).strip() for r in dados}

    novos: list[dict] = []
    sess = session()
    hoje = date.today()
    datas = [hoje - timedelta(days=i) for i in range(dias)]

    for dia in datas:
        for secao in SECOES:
            try:
                links = buscar_links(sess, dia, termo, secao)
            except Exception as exc:
                print(f"[aviso] falha na busca {dia} {secao}: {exc}", file=sys.stderr)
                continue
            print(f"{dia} {secao}: {len(links)} link(s) candidato(s)")
            for link in sorted(links):
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
                novos.append(d)
                links_existentes.add(link)
                print(f"+ {d['data']} | {d['titulo']} | {d['link']}")

    if not novos:
        print("Nenhuma portaria nova encontrada.")
        return 0

    dados_atualizados = dados + novos
    # Deduplicação final por link.
    por_link: dict[str, dict] = {}
    for r in dados_atualizados:
        link = str(r.get("link", "")).strip()
        if link:
            por_link[link] = r
    dados_atualizados = list(por_link.values())
    dados_atualizados.sort(key=lambda r: (data_key(r), int(re.sub(r"\D", "", str(r.get("numero", "0"))) or 0)), reverse=True)

    novo_html = substituir_const_data(html_atual, dados_atualizados)
    index_path.write_text(novo_html, encoding="utf-8")
    print(f"Atualização concluída: {len(novos)} novo(s) registro(s). Total: {len(dados_atualizados)}.")
    return len(novos)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index.html", help="Caminho para o index.html")
    ap.add_argument("--dias", type=int, default=3, help="Quantidade de dias recentes a consultar")
    ap.add_argument("--termo", default=DEFAULT_TERMO, help="Termo de busca no DOU")
    ap.add_argument("--pausa", type=float, default=0.7, help="Pausa entre leituras de páginas")
    args = ap.parse_args()
    atualizar(Path(args.index), args.dias, args.termo, args.pausa)


if __name__ == "__main__":
    main()
