#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza o index.html do site de Portarias MESP usando o buscador estruturado do DOU.

Regra principal: NÃO apaga registros antigos.
O script lê a lista DATA já existente no index.html, busca novas ocorrências de
"portaria MESP" no DOU, junta tudo, remove duplicidades por link e regrava o HTML.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

IN_API_BASE_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
IN_WEB_BASE_URL = "https://www.in.gov.br/web/dou/-/"

SCRIPT_ID = "_br_com_seatecnologia_in_buscadou_BuscaDouPortlet_params"


def limpar_html(texto_html):
    if texto_html is None:
        return ""
    return BeautifulSoup(str(texto_html), "html.parser").get_text(" ", strip=True)


def normalizar(texto):
    try:
        import unicodedata
        texto = unicodedata.normalize("NFD", str(texto or ""))
        texto = "".join(ch for ch in texto if unicodedata.category(ch) != "Mn")
    except Exception:
        texto = str(texto or "")
    return texto.upper().strip()


def eh_portaria_mesp(item):
    texto = " ".join(
        str(item.get(campo, "") or "")
        for campo in ["title", "hierarchyStr", "content", "urlTitle", "artType"]
    )
    texto_norm = normalizar(limpar_html(texto))
    return "PORTARIA" in texto_norm and "MESP" in texto_norm


def extrair_numero_portaria(titulo):
    texto = str(titulo or "")
    padroes = [
        r"PORTARIA\s+MESP\s*(?:N[º°oO\.]*)?\s*([0-9][0-9\./-]*)",
        r"PORTARIA\s+MESP\s+([0-9][0-9\./-]*)",
    ]
    for padrao in padroes:
        m = re.search(padrao, texto, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def parse_data_br(data_txt):
    if not data_txt:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(data_txt)[:10], fmt).date()
        except Exception:
            pass
    return None


def data_ordenacao(registro):
    d = parse_data_br(registro.get("data"))
    if d:
        return d.strftime("%Y%m%d")
    return "00000000"


def requisitar_pagina(payload):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; PesquisaDOU-GitHubActions/1.0; +https://www.in.gov.br)"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    resposta = requests.get(IN_API_BASE_URL, params=payload, headers=headers, timeout=40)
    resposta.raise_for_status()
    return resposta


def extrair_resultados_da_pagina(html):
    soup = BeautifulSoup(html, "html.parser")
    script_tag = soup.find("script", id=SCRIPT_ID)

    if script_tag is None:
        return [], soup

    conteudo = script_tag.string or "".join(script_tag.contents)
    dados = json.loads(conteudo)
    return dados.get("jsonArray", []), soup


def descobrir_numero_paginas(soup):
    last_page = soup.find("button", id="lastPage")
    if last_page is not None:
        try:
            return int(last_page.text.strip())
        except Exception:
            return 1

    second_page = soup.find("button", id="2btn")
    if second_page is not None:
        return 2

    return 1


def buscar_dou(termo, dias, secoes=None, busca_exata=True, pausa=1.0):
    if secoes is None:
        secoes = ["todos"]

    data_final = datetime.today()
    data_inicial = data_final - timedelta(days=max(0, dias - 1))

    query = f'"{termo}"' if busca_exata else termo

    payload = {
        "q": query,
        "exactDate": "personalizado",
        "publishFrom": data_inicial.strftime("%d-%m-%Y"),
        "publishTo": data_final.strftime("%d-%m-%Y"),
        "sortType": "0",
        "s": secoes,
    }

    print("Pesquisando no DOU")
    print("Termo:", payload["q"])
    print("Período:", payload["publishFrom"], "até", payload["publishTo"])
    print("Seções:", ", ".join(secoes))
    print("-" * 80)

    resposta = requisitar_pagina(payload)
    resultados_pagina, soup = extrair_resultados_da_pagina(resposta.content)
    numero_paginas = descobrir_numero_paginas(soup)

    print("Páginas estimadas:", numero_paginas)
    print("Resultados na primeira página:", len(resultados_pagina))

    todos_resultados = []
    ultimo_item = None

    for item in resultados_pagina:
        todos_resultados.append(item)
        ultimo_item = item

    for pagina in range(2, numero_paginas + 1):
        if ultimo_item is None:
            break

        payload.update({
            "id": ultimo_item.get("classPK"),
            "displayDate": ultimo_item.get("displayDateSortable"),
            "newPage": pagina,
            "currentPage": pagina - 1,
        })

        print(f"Coletando página {pagina} de {numero_paginas}...")
        try:
            resposta = requisitar_pagina(payload)
            resultados_pagina, soup = extrair_resultados_da_pagina(resposta.content)
        except Exception as exc:
            print(f"Erro ao coletar página {pagina}: {exc}")
            continue

        for item in resultados_pagina:
            todos_resultados.append(item)
            ultimo_item = item

        time.sleep(pausa)

    registros = []
    for item in todos_resultados:
        if not eh_portaria_mesp(item):
            continue

        data = item.get("pubDate") or ""
        d = parse_data_br(data)
        titulo = item.get("title") or ""
        link = IN_WEB_BASE_URL + (item.get("urlTitle") or "")

        registros.append({
            "data": data,
            "ano": d.year if d else "",
            "mes": d.month if d else "",
            "secao": item.get("pubName") or "",
            "tipo": item.get("artType") or "",
            "titulo": titulo,
            "numero": extrair_numero_portaria(titulo + " " + link),
            "orgao": item.get("hierarchyStr") or "",
            "resumo": limpar_html(item.get("content", "")),
            "link": link,
        })

    print("Portarias MESP encontradas no período:", len(registros))
    return registros


def extrair_array_data(html):
    marcador = "const DATA ="
    pos = html.find(marcador)
    if pos == -1:
        raise RuntimeError("Não encontrei 'const DATA =' no index.html.")

    inicio = html.find("[", pos)
    if inicio == -1:
        raise RuntimeError("Não encontrei o início '[' da lista DATA.")

    dentro_string = False
    escape = False
    profundidade = 0

    for i in range(inicio, len(html)):
        ch = html[i]

        if dentro_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                dentro_string = False
            continue

        if ch == '"':
            dentro_string = True
        elif ch == "[":
            profundidade += 1
        elif ch == "]":
            profundidade -= 1
            if profundidade == 0:
                fim = i + 1
                return inicio, fim, json.loads(html[inicio:fim])

    raise RuntimeError("Não consegui localizar o fim da lista DATA.")


def chave_registro(registro):
    link = str(registro.get("link") or "").strip()
    if link:
        return "link:" + link

    base = "|".join(str(registro.get(k, "") or "").strip().upper() for k in ["data", "titulo", "orgao"])
    return "base:" + base


def juntar_sem_apagar(atuais, novos):
    combinados = []
    vistos = set()

    # Mantém todos os registros antigos.
    for reg in atuais:
        chave = chave_registro(reg)
        if chave not in vistos:
            vistos.add(chave)
            combinados.append(reg)

    adicionados = 0
    for reg in novos:
        chave = chave_registro(reg)
        if chave not in vistos:
            vistos.add(chave)
            combinados.append(reg)
            adicionados += 1

    combinados.sort(key=lambda r: (data_ordenacao(r), str(r.get("numero") or "")), reverse=True)
    return combinados, adicionados


def atualizar_kpis(html, registros):
    anos = {r.get("ano") for r in registros if r.get("ano")}
    secoes = {r.get("secao") for r in registros if r.get("secao")}
    orgaos = {r.get("orgao") for r in registros if r.get("orgao")}

    substituicoes = {
        "kpiTotal": len(registros),
        "kpiAnos": len(anos),
        "kpiSecoes": len(secoes),
        "kpiOrgaos": len(orgaos),
    }

    for kpi_id, valor in substituicoes.items():
        html = re.sub(
            rf'(<div class="n" id="{re.escape(kpi_id)}">)(.*?)(</div>)',
            rf'\g<1>{valor}\g<3>',
            html,
            count=1,
            flags=re.DOTALL,
        )

    return html


def atualizar_html(index_path, registros):
    html = index_path.read_text(encoding="utf-8")
    inicio, fim, _atuais = extrair_array_data(html)

    novo_json = json.dumps(registros, ensure_ascii=False, separators=(",", ":"))
    html = html[:inicio] + novo_json + html[fim:]
    html = atualizar_kpis(html, registros)

    index_path.write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="index.html", help="Caminho para o index.html")
    parser.add_argument("--dias", type=int, default=10, help="Quantos dias para trás pesquisar")
    parser.add_argument("--termo", default="portaria MESP", help="Termo de busca no DOU")
    parser.add_argument("--busca-aberta", action="store_true", help="Usar busca sem aspas")
    args = parser.parse_args()

    index_path = Path(args.index)
    if not index_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {index_path}")

    html = index_path.read_text(encoding="utf-8")
    _inicio, _fim, atuais = extrair_array_data(html)
    print("Registros já existentes no index.html:", len(atuais))

    novos = buscar_dou(
        termo=args.termo,
        dias=args.dias,
        secoes=["todos"],
        busca_exata=not args.busca_aberta,
    )

    combinados, adicionados = juntar_sem_apagar(atuais, novos)
    print("Novos registros adicionados:", adicionados)
    print("Total após atualização:", len(combinados))

    if adicionados == 0:
        print("Nenhum registro novo a acrescentar. O index.html não será alterado.")
        return 0

    atualizar_html(index_path, combinados)
    print("index.html atualizado com sucesso, sem apagar registros anteriores.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
