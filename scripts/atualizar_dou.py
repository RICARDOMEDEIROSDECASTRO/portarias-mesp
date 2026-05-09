#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Atualizador automático da base de atos do DOU relacionados a "portaria MESP".

O script:
1. Lê os registros já existentes no const DATA do index.html.
2. Consulta o buscador estruturado do DOU, usando o mesmo padrão que funcionou no Colab.
3. Limpa HTML de destaque do DOU, como <span class='highlight'>.
4. Classifica os atos por categoria: Portaria, Deliberação, Extrato etc.
5. Acrescenta apenas registros novos.
6. Não apaga registros antigos.
7. Também saneia registros antigos já existentes, removendo HTML indevido de título, órgão e resumo.
8. Regrava o index.html somente se houver alteração.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


IN_API_BASE_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
IN_WEB_BASE_URL = "https://www.in.gov.br/web/dou/-/"
SCRIPT_ID = "_br_com_seatecnologia_in_buscadou_BuscaDouPortlet_params"


# ============================================================
# Funções de limpeza e normalização
# ============================================================

def limpar_html(texto_html):
    """
    Remove tags HTML eventualmente devolvidas pelo DOU.
    Exemplo: <span class='highlight'>PORTARIA</span> vira PORTARIA.
    """
    if texto_html is None:
        return ""
    return BeautifulSoup(str(texto_html), "html.parser").get_text(" ", strip=True)


def limpar_espacos(texto):
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def normalizar(texto):
    texto = limpar_html(texto)
    texto = unicodedata.normalize("NFD", str(texto or ""))
    texto = "".join(ch for ch in texto if unicodedata.category(ch) != "Mn")
    texto = texto.upper()
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def parse_data_br(data_txt):
    if not data_txt:
        return None

    data_txt = str(data_txt).strip()[:10]

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(data_txt, fmt).date()
        except Exception:
            pass

    return None


def data_ordenacao(registro):
    d = parse_data_br(registro.get("data"))
    if d:
        return d.strftime("%Y%m%d")
    return "00000000"


# ============================================================
# Classificação dos atos
# ============================================================

def classificar_tipo_ato_por_texto(titulo, tipo=""):
    titulo_norm = normalizar(titulo)
    tipo_norm = normalizar(tipo)

    if "PORTARIA" in titulo_norm or tipo_norm == "PORTARIA":
        return "Portaria"

    if "DELIBERACAO" in titulo_norm or tipo_norm == "DELIBERACAO":
        return "Deliberação"

    if "RETIFICACAO" in titulo_norm or tipo_norm == "RETIFICACAO":
        return "Retificação"

    if "EXTRATO DE TERMO DE PARCELAMENTO" in titulo_norm:
        return "Extrato de Termo de Parcelamento de Débito"

    if "EXTRATO DE PARCELAMENTO" in titulo_norm:
        return "Extrato de Parcelamento de Débito"

    if "CESSAO DE USO" in titulo_norm:
        return "Extrato de Cessão de Uso"

    if "AUTORIZACAO DE USO" in titulo_norm:
        return "Extrato de Autorização de Uso"

    if "EXTRATO" in titulo_norm or tipo_norm == "EXTRATO":
        return "Extrato"

    return "Outros"


def classificar_tipo_ato(item):
    titulo = limpar_html(item.get("title") or "")
    tipo = limpar_html(item.get("artType") or "")
    return classificar_tipo_ato_por_texto(titulo, tipo)


def classificar_natureza_portaria(titulo, resumo, categoria_ato):
    """
    Classificação preliminar para futura diferenciação entre portarias normativas
    relevantes e portarias administrativas.

    Esta classificação é apenas auxiliar e conservadora.
    """
    if categoria_ato != "Portaria":
        return ""

    texto = normalizar(f"{titulo} {resumo}")

    termos_normativos = [
        "REGULAMENTA",
        "DISPOE SOBRE",
        "ESTABELECE",
        "INSTITUI",
        "APROVA",
        "ALTERA",
        "REVOGA",
        "CRITERIOS",
        "DIRETRIZES",
        "PROCEDIMENTO",
        "PROCEDIMENTOS",
        "FLUXO",
        "REGIMENTO INTERNO",
        "PROGRAMA",
        "COMITE",
        "POLITICA",
        "PLANO",
        "NORMAS",
        "ORIENTACOES",
    ]

    termos_pessoal = [
        "NOMEAR",
        "EXONERAR",
        "DESIGNAR",
        "DISPENSAR",
        "CEDER",
        "CESSAO",
        "CARGO COMISSIONADO",
        "FUNCAO COMISSIONADA",
        "SUBSTITUTO",
        "SUBSTITUTA",
    ]

    if any(t in texto for t in termos_normativos):
        return "Normativa / programática"

    if any(t in texto for t in termos_pessoal):
        return "Pessoal / cargos"

    if "BOLSA ATLETA" in texto or "CONTEMPLA ATLETAS" in texto:
        return "Bolsa Atleta"

    if "LEI DE INCENTIVO" in texto or "PROJETOS DESPORTIVOS" in texto:
        return "Lei de Incentivo ao Esporte"

    return "Administrativa / outras"


# ============================================================
# Extração de número
# ============================================================

def extrair_numero_portaria(titulo_ou_link):
    texto = limpar_html(titulo_ou_link)
    texto_norm = normalizar(texto)

    padroes = [
        r"PORTARIA\s+MESP\s*/\s*SE\s*(?:N[º°O\.]*)?\s*([0-9][0-9\./-]*)",
        r"PORTARIA\s+MESP\s*(?:N[º°O\.]*)?\s*([0-9][0-9\./-]*)",
        r"PORTARIA\s*(?:N[º°O\.]*)?\s*([0-9][0-9\./-]*)\s+MESP\s*/\s*SE",
        r"PORTARIA\s*(?:N[º°O\.]*)?\s*([0-9][0-9\./-]*)",
    ]

    for padrao in padroes:
        m = re.search(padrao, texto_norm, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()

    return ""


# ============================================================
# Sessão HTTP e busca no DOU
# ============================================================

def criar_sessao():
    sessao = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    sessao.mount("https://", adapter)
    sessao.mount("http://", adapter)

    sessao.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    })

    return sessao


def requisitar_pagina(sessao, payload):
    ultimo_erro = None

    for tentativa in range(1, 6):
        try:
            print(f"Tentativa {tentativa}/5...")
            resposta = sessao.get(
                IN_API_BASE_URL,
                params=payload,
                timeout=60,
            )

            print("Status:", resposta.status_code)
            print("URL:", resposta.url)

            resposta.raise_for_status()
            return resposta

        except Exception as exc:
            ultimo_erro = exc
            espera = tentativa * 10
            print(f"[aviso] falha na tentativa {tentativa}: {exc}")
            print(f"Aguardando {espera} segundos antes de tentar novamente...")
            time.sleep(espera)

    raise RuntimeError(f"Falha definitiva ao consultar o DOU: {ultimo_erro}")


def extrair_resultados_da_pagina(html):
    soup = BeautifulSoup(html, "html.parser")

    script_tag = soup.find("script", id=SCRIPT_ID)

    if script_tag is None:
        print("Não encontrei o bloco JSON estruturado do DOU.")
        print("Início do HTML recebido:")
        print(str(soup)[:1500])
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


def buscar_dou(termo, dias, busca_exata=True):
    data_final = datetime.today()
    data_inicial = data_final - timedelta(days=max(0, dias - 1))

    query = f'"{termo}"' if busca_exata else termo

    payload = {
        "q": query,
        "exactDate": "personalizado",
        "publishFrom": data_inicial.strftime("%d-%m-%Y"),
        "publishTo": data_final.strftime("%d-%m-%Y"),
        "sortType": "0",
        "s": ["todos"],
    }

    print("Pesquisando no DOU...")
    print("Termo:", payload["q"])
    print("Período:", payload["publishFrom"], "até", payload["publishTo"])
    print("Seções: todos")
    print("-" * 80)

    sessao = criar_sessao()

    resposta = requisitar_pagina(sessao, payload)
    resultados_pagina, soup = extrair_resultados_da_pagina(resposta.content)

    numero_paginas = descobrir_numero_paginas(soup)

    print("Número estimado de páginas:", numero_paginas)
    print("Resultados na primeira página:", len(resultados_pagina))
    print("-" * 80)

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
        time.sleep(5)

        try:
            resposta = requisitar_pagina(sessao, payload)
            resultados_pagina, soup = extrair_resultados_da_pagina(resposta.content)
        except Exception as exc:
            print(f"[aviso] erro ao coletar página {pagina}: {exc}")
            continue

        print("Resultados nesta página:", len(resultados_pagina))

        for item in resultados_pagina:
            todos_resultados.append(item)
            ultimo_item = item

    registros = []

    for item in todos_resultados:
        titulo = limpar_espacos(limpar_html(item.get("title") or ""))
        tipo = limpar_espacos(limpar_html(item.get("artType") or ""))
        orgao = limpar_espacos(limpar_html(item.get("hierarchyStr") or ""))
        resumo = limpar_espacos(limpar_html(item.get("content", "")))
        data = item.get("pubDate") or ""
        d = parse_data_br(data)
        link = IN_WEB_BASE_URL + (item.get("urlTitle") or "")
        categoria_ato = classificar_tipo_ato(item)

        # A busca é por "portaria MESP"; mantemos atos que tenham essa expressão
        # no título ou no conteúdo, pois podem ser Deliberações, Extratos etc.
        texto_busca = normalizar(f"{titulo} {resumo} {orgao} {link}")
        if "PORTARIA" not in texto_busca or "MESP" not in texto_busca:
            continue

        registro = {
            "data": data,
            "ano": d.year if d else "",
            "mes": d.month if d else "",
            "secao": item.get("pubName") or "",
            "tipo": tipo,
            "categoria_ato": categoria_ato,
            "natureza_portaria": classificar_natureza_portaria(titulo, resumo, categoria_ato),
            "numero": extrair_numero_portaria(titulo + " " + link),
            "titulo": titulo,
            "orgao": orgao,
            "resumo": resumo,
            "link": link,
        }

        registros.append(registro)

    print("Atos relacionados a Portaria MESP encontrados no período:", len(registros))

    return registros


# ============================================================
# Leitura e atualização do const DATA no index.html
# ============================================================

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
    """
    Chave robusta para evitar duplicidade por pequena diferença de link.
    Prioriza data + título + órgão; usa link como fallback.
    """
    data = str(registro.get("data") or "").strip()
    titulo = normalizar(registro.get("titulo") or "")
    orgao = normalizar(registro.get("orgao") or "")
    categoria = normalizar(registro.get("categoria_ato") or registro.get("tipo") or "")
    numero = normalizar(registro.get("numero") or "")

    if data and titulo:
        return "ato:" + "|".join([data, titulo, orgao, categoria, numero])

    link = str(registro.get("link") or "").strip()
    if link:
        return "link:" + link

    base = "|".join(
        str(registro.get(k, "") or "").strip().upper()
        for k in ["data", "titulo", "orgao"]
    )

    return "base:" + base


def sanear_registro_antigo(registro):
    """
    Saneia registros já existentes no index.html:
    - remove HTML de título, órgão, tipo e resumo;
    - acrescenta categoria_ato;
    - acrescenta natureza_portaria;
    - preserva todos os campos antigos.
    """
    reg = dict(registro)

    titulo = limpar_espacos(limpar_html(reg.get("titulo") or ""))
    tipo = limpar_espacos(limpar_html(reg.get("tipo") or ""))
    orgao = limpar_espacos(limpar_html(reg.get("orgao") or ""))
    resumo = limpar_espacos(limpar_html(reg.get("resumo") or ""))

    reg["titulo"] = titulo
    reg["tipo"] = tipo
    reg["orgao"] = orgao
    reg["resumo"] = resumo

    if not reg.get("categoria_ato"):
        reg["categoria_ato"] = classificar_tipo_ato_por_texto(titulo, tipo)

    if not reg.get("natureza_portaria"):
        reg["natureza_portaria"] = classificar_natureza_portaria(
            titulo,
            resumo,
            reg.get("categoria_ato") or "",
        )

    if not reg.get("numero"):
        reg["numero"] = extrair_numero_portaria(titulo + " " + str(reg.get("link") or ""))

    d = parse_data_br(reg.get("data"))
    if d:
        reg["ano"] = reg.get("ano") or d.year
        reg["mes"] = reg.get("mes") or d.month

    return reg


def juntar_sem_apagar(atuais, novos):
    combinados = []
    vistos = set()

    for reg in atuais:
        reg_saneado = sanear_registro_antigo(reg)
        chave = chave_registro(reg_saneado)

        if chave not in vistos:
            vistos.add(chave)
            combinados.append(reg_saneado)

    adicionados = 0

    for reg in novos:
        reg_saneado = sanear_registro_antigo(reg)
        chave = chave_registro(reg_saneado)

        if chave not in vistos:
            vistos.add(chave)
            combinados.append(reg_saneado)
            adicionados += 1

    combinados.sort(
        key=lambda r: (data_ordenacao(r), str(r.get("numero") or "")),
        reverse=True,
    )

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

    novo_json = json.dumps(
        registros,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    html = html[:inicio] + novo_json + html[fim:]
    html = atualizar_kpis(html, registros)

    index_path.write_text(html, encoding="utf-8")


# ============================================================
# Execução principal
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="index.html")
    parser.add_argument("--dias", type=int, default=3)
    parser.add_argument("--termo", default="portaria MESP")
    parser.add_argument("--busca-aberta", action="store_true")

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
        busca_exata=not args.busca_aberta,
    )

    combinados, adicionados = juntar_sem_apagar(atuais, novos)

    print("Novos registros adicionados:", adicionados)
    print("Total após atualização:", len(combinados))

    # Mesmo sem novos registros, pode haver saneamento dos antigos,
    # como remoção de <span class='highlight'> nos títulos.
    html_atual = index_path.read_text(encoding="utf-8")
    _i, _f, atuais_saneados_teste = extrair_array_data(html_atual)

    houve_saneamento = json.dumps(atuais_saneados_teste, ensure_ascii=False, separators=(",", ":")) != json.dumps(combinados, ensure_ascii=False, separators=(",", ":"))

    if adicionados == 0 and not houve_saneamento:
        print("Nenhum registro novo ou saneamento necessário. O index.html não será alterado.")
        return 0

    atualizar_html(index_path, combinados)

    if adicionados > 0:
        print("index.html atualizado com novos registros, sem apagar registros anteriores.")
    else:
        print("index.html saneado, sem acréscimo de novos registros.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
