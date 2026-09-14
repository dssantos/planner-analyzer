"""Servidor Flask do Analisador de Planilhas do Microsoft Planner.

Rotas:
  GET  /              -> página única (upload + dashboard + resumo)
  POST /api/upload    -> valida e carrega a planilha (.xlsx)
  GET  /api/dados     -> retorna dados + lookups para o dashboard
  POST /api/resumo    -> gera resumo de conteúdo via LLM, com filtros aplicados
"""
from __future__ import annotations

import calendar
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any

from flask import Flask, jsonify, render_template, request, session

import llm
import planner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app")

app = Flask(__name__)
# Chave de sessão (em produção, usar variável de ambiente).
app.secret_key = os.getenv("FLASK_SECRET_KEY", "planner-analyzer-dev-key")

# Diretório temporário para os arquivos enviados.
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "planner_analyzer_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# Utilitários de sessão
# ----------------------------------------------------------------------------

def _caminho_arquivo_sessao() -> str | None:
    """Retorna o caminho do último arquivo válido carregado na sessão."""
    caminho = session.get("arquivo")
    if caminho and os.path.exists(caminho):
        return caminho
    return None


# ----------------------------------------------------------------------------
# Filtragem server-side
# ----------------------------------------------------------------------------

def _resolver_periodo(spec: dict[str, Any] | None) -> tuple[str | None, str | None, str]:
    """Converte um spec de período {preset|range} em (inicio, fim, descricao).

    Retorna datas no formato 'YYYY-MM-DD' (ou None para sem limite).
    """
    if not spec:
        return None, None, "todo o período"

    hoje = datetime.now().date()

    if spec.get("tipo") == "range":
        ini = spec.get("inicio") or None
        fim = spec.get("fim") or None
        desc = "intervalo personalizado"
        if ini and fim:
            desc = f"{ini} a {fim}"
        elif ini:
            desc = f"a partir de {ini}"
        elif fim:
            desc = f"até {fim}"
        return ini, fim, desc

    # Presets
    preset = (spec.get("preset") or "tudo").lower()
    if preset == "tudo":
        return None, None, "todo o período"
    if preset == "7":
        ini = (hoje - timedelta(days=7)).isoformat()
        return ini, hoje.isoformat(), "últimos 7 dias"
    if preset == "30":
        ini = (hoje - timedelta(days=30)).isoformat()
        return ini, hoje.isoformat(), "últimos 30 dias"
    if preset == "90":
        ini = (hoje - timedelta(days=90)).isoformat()
        return ini, hoje.isoformat(), "últimos 90 dias"
    if preset == "mes":
        # "Este mês" = mês-calendário inteiro (1º ao último dia), não até hoje,
        # para incluir tarefas com data de conclusão futura dentro do mês.
        ini = hoje.replace(day=1).isoformat()
        fim = hoje.replace(day=calendar.monthrange(hoje.year, hoje.month)[1]).isoformat()
        return ini, fim, "este mês"
    if preset == "mes_anterior":
        primeiro_deste = hoje.replace(day=1)
        ultimo_anterior = primeiro_deste - timedelta(days=1)
        ini = ultimo_anterior.replace(day=1).isoformat()
        fim = ultimo_anterior.isoformat()
        return ini, fim, "mês anterior"
    return None, None, "todo o período"


def _data_dentro(valor_iso: str | None, ini: str | None, fim: str | None) -> bool:
    """Verifica se uma data ISO está dentro do intervalo [ini, fim].

    Se a data for None: só passa se não houver filtro de período (Tudo).
    """
    if ini is None and fim is None:
        return True
    if not valor_iso:
        return False
    if ini and valor_iso < ini:
        return False
    if fim and valor_iso > fim:
        return False
    return True


def filtrar_tarefas(
    dados: dict[str, Any],
    filtros: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aplica os filtros às tarefas e retorna (lista_filtrada, contexto_descrito).

    Contexto descrito é usado pelo LLM para entender os filtros aplicados.
    """
    tarefas = dados["tarefas"]

    pessoas_sel = set(filtros.get("pessoas") or [])
    categorias_sel = set(filtros.get("categorias") or [])
    status_sel = set(filtros.get("status") or [])
    rotulos_sel = set(filtros.get("rotulos") or [])

    # Único período: data de conclusão (computada: "Data de conclusão" c/ fallback
    # "Concluído em"). Padrão do app: últimos 30 dias.
    ini_concl, fim_concl, desc_concl = _resolver_periodo(filtros.get("data_conclusao"))

    resultado = []
    for t in tarefas:
        # Pessoas: usa os responsáveis computados (Atribuído a > Concluída por > Criado por).
        if pessoas_sel:
            pessoas_tarefa = set(t.get("responsaveis") or [])
            if not (pessoas_sel & pessoas_tarefa):
                continue

        if categorias_sel and t.get("categoria") not in categorias_sel:
            continue

        if status_sel and t.get("status") not in status_sel:
            continue

        # Rótulos podem ser multi-valorados (separados por ';').
        # O sentinela "__sem_rotulo__" filtra registros que NÃO possuem rótulo.
        if rotulos_sel:
            rotulos_tarefa = {
                r.strip() for r in str(t.get("rotulos") or "").split(";") if r.strip()
            }
            sem_rotulo_sel = "__sem_rotulo__" in rotulos_sel
            rotulos_reais = rotulos_sel - {"__sem_rotulo__"}
            bate = bool(rotulos_reais & rotulos_tarefa)
            if not bate and sem_rotulo_sel and not rotulos_tarefa:
                bate = True
            if not bate:
                continue

        # Período de conclusão (campo computado data_conclusao).
        if (ini_concl is not None or fim_concl is not None) and not _data_dentro(
            t.get("data_conclusao"), ini_concl, fim_concl
        ):
            continue

        resultado.append(t)

    # Remove o sentinela antes de repassar ao contexto (LLM não deve vê-lo).
    rotulos_para_contexto = sorted(r for r in rotulos_sel if r != "__sem_rotulo__")
    if "__sem_rotulo__" in rotulos_sel:
        rotulos_para_contexto.append("(sem rótulo)")

    contexto = {
        "pessoas": sorted(pessoas_sel),
        "categorias": sorted(categorias_sel),
        "status": sorted(status_sel),
        "rotulos": rotulos_para_contexto,
        "periodo_descricao": desc_concl,
    }
    return resultado, contexto


# ----------------------------------------------------------------------------
# Rotas
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "arquivo" not in request.files:
        return jsonify({"ok": False, "erros": ["Nenhum arquivo enviado."], "avisos": []}), 400

    arquivo = request.files["arquivo"]
    if not arquivo.filename:
        return jsonify({"ok": False, "erros": ["Nenhum arquivo selecionado."], "avisos": []}), 400

    if not arquivo.filename.lower().endswith(".xlsx"):
        return jsonify({
            "ok": False,
            "erros": ["O arquivo deve ser no formato .xlsx (planilha do Excel)."],
            "avisos": [],
        }), 400

    # Salva em arquivo temporário com nome único.
    nome_seguro = f"{uuid.uuid4().hex}_{arquivo.filename}"
    caminho = os.path.join(UPLOAD_DIR, nome_seguro)
    arquivo.save(caminho)

    ok, erros, avisos = planner.validar_planilha(caminho)
    if not ok:
        # Remove arquivo inválido.
        try:
            os.remove(caminho)
        except OSError:
            pass
        return jsonify({"ok": False, "erros": erros, "avisos": avisos}), 422

    # Guarda o caminho na sessão para as próximas chamadas.
    session["arquivo"] = caminho
    logger.info("Planilha válida carregada: %s", caminho)
    return jsonify({"ok": True, "erros": [], "avisos": avisos})


@app.route("/api/dados", methods=["GET"])
def dados():
    caminho = _caminho_arquivo_sessao()
    if not caminho:
        return jsonify({"ok": False, "erro": "Nenhuma planilha carregada. Envie um arquivo primeiro."}), 400

    try:
        dados = planner.carregar_dados(caminho)
    except Exception as exc:
        logger.exception("Erro ao carregar dados")
        return jsonify({"ok": False, "erro": f"Erro ao ler a planilha: {exc}"}), 500

    return jsonify({"ok": True, "dados": dados})


@app.route("/api/resumo", methods=["POST"])
def resumo():
    caminho = _caminho_arquivo_sessao()
    if not caminho:
        return jsonify({"ok": False, "erro": "Nenhuma planilha carregada. Envie um arquivo primeiro."}), 400

    try:
        dados = planner.carregar_dados(caminho)
    except Exception as exc:
        logger.exception("Erro ao carregar dados para resumo")
        return jsonify({"ok": False, "erro": f"Erro ao ler a planilha: {exc}"}), 500

    corpo = request.get_json(silent=True) or {}
    filtros = corpo.get("filtros") or {}
    tamanho = corpo.get("tamanho", "curto")
    tom = corpo.get("tom", "executivo")
    pessoa = corpo.get("pessoa", "terceira")

    if tamanho not in llm.TAMANHOS:
        tamanho = "curto"
    if tom not in ("executivo", "narrativo"):
        tom = "executivo"
    if pessoa not in ("primeira", "terceira"):
        pessoa = "terceira"

    tarefas_filtradas, contexto = filtrar_tarefas(dados, filtros)

    # Define singular/plural pela quantidade de pessoas selecionadas no FILTRO.
    # Se nenhuma estiver selecionada, considera todas as pessoas envolvidas nas
    # tarefas filtradas (atribuídos) para nomear a equipe.
    pessoas_filtro = sorted(p for p in (filtros.get("pessoas") or []) if p)
    if pessoas_filtro:
        lista_pessoas = pessoas_filtro
    else:
        envolvidas: set[str] = set()
        for t in tarefas_filtradas:
            for p in (t.get("responsaveis") or []):
                if p:
                    envolvidas.add(p)
        lista_pessoas = sorted(envolvidas)
    contexto["pessoas_envolvidas"] = lista_pessoas

    logger.info(
        "Resumo solicitado: %d tarefas, %d pessoa(s) no filtro, tamanho=%s, tom=%s, pessoa=%s",
        len(tarefas_filtradas), len(lista_pessoas), tamanho, tom, pessoa,
    )

    try:
        resultado = llm.gerar_resumo(
            tarefas_filtradas, contexto, tamanho=tamanho, tom=tom, pessoa=pessoa
        )
    except Exception as exc:
        logger.exception("Erro ao gerar resumo")
        return jsonify({"ok": False, "erro": f"Erro ao gerar resumo: {exc}"}), 500

    return jsonify({"ok": True, "resultado": resultado, "n_tarefas_filtradas": len(tarefas_filtradas)})


if __name__ == "__main__":
    # HOST/PORT/DEBUG configuráveis via ambiente. Defaults preservam o uso local
    # (127.0.0.1:5000, debug ligado). Em container, defina HOST=0.0.0.0.
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "1").lower() in ("1", "true", "yes", "on")
    print("\n  Analisador de Planilhas do Microsoft Planner")
    print(f"  Acesse: http://localhost:{port}  (bind {host}, debug={debug})\n")
    app.run(host=host, port=port, debug=debug)
