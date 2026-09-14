"""Geração de resumo de atividades via LLM (multi-provedor/multi-modelo).

Estratégia:
  1. Tenta provedores/modelos gratuitos primeiro (ZAI), depois DeepSeek.
  2. O resumo foca no CONTEÚDO das tarefas filtradas: "Nome da tarefa",
     "Itens da lista de verificação" e "Notas", considerando o contexto dos
     filtros (quem, quando, status, categoria).
  3. Para volumes grandes de conteúdo, aplica resumo em cascata (map-reduce):
     agrupa tarefas em lotes, resume cada lote e depois consolida.

Modelos tentados, em ordem:
  ZAI:     glm-4.7-flash, glm-4-flash-250414, glm-4-flash
  DeepSeek: deepseek-chat, deepseek-reasoner
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("llm")

# ----------------------------------------------------------------------------
# Configuração de provedores e modelos
# ----------------------------------------------------------------------------

# Cada entrada: (provedor, endpoint, nome_do_modelo, env_var_da_chave, limite_tokens_entrada)
# Limite de entrada aproximado para decidir quando aplicar map-reduce.
# Ordem de fallback (todos gratuitos ou com bônus):
#   1. Gemini gemini-3.5-flash         — gratuito (Google AI Studio), mais recente
#   2. Gemini gemini-2.5-flash         — gratuito, 1M ctx
#   3. ZAI glm-4.5-flash (global)      — gratuito, ~5-11s
#   4. ZAI glm-4.5-flash (China)       — gratuito, ~13-23s (fallback lento)
#   5. DeepSeek deepseek-v4-flash      — bônus 5M tokens, ~5s
#   6. DeepSeek deepseek-chat          — bônus 5M tokens, ~5s
PROVEDORES: list[tuple[str, str, str, str, int]] = [
    ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-3.5-flash", "GEMINI_API_KEY", 120_000),
    ("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-2.5-flash", "GEMINI_API_KEY", 120_000),
    ("ZAI", "https://api.z.ai/api/paas/v4/chat/completions", "glm-4.5-flash", "ZAI_API_KEY", 120_000),
    ("ZAI", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.5-flash", "ZAI_API_KEY", 120_000),
    ("DeepSeek", "https://api.deepseek.com/chat/completions", "deepseek-v4-flash", "DEEPSEEK_API_KEY", 60_000),
    ("DeepSeek", "https://api.deepseek.com/chat/completions", "deepseek-chat", "DEEPSEEK_API_KEY", 60_000),
]

# Tamanhos de resumo (palavras-alvo, múltiplos de 100).
# Reduzidos em ~5x em relação aos valores originais para respostas mais concisas.
TAMANHOS = {
    "curto": 100,
    "medio": 200,
    "longo": 400,
    "muito_longo": 700,
}

# Em PT-BR, ~1 token ≈ 0,65 palavra (≈1,5 tokens/palavra).
TOKENS_POR_PALAVRA = 1.5
# Tamanho de lote (nº de tarefas por chamada no map-reduce).
TAMANHO_LOTE = 25
# Limite de caracteres por campo de tarefa (evita uma Nota gigante dominar o prompt).
LIMITE_CHAR_CAMPO = 1500
TIMEOUT_SEGUNDOS = 90

_session = requests.Session()


# ----------------------------------------------------------------------------
# Utilitários
# ----------------------------------------------------------------------------

def _estimar_tokens(texto: str) -> int:
    """Estima o número de tokens de um texto (aproximação simples)."""
    return int(len(texto.split()) * TOKENS_POR_PALAVRA) + 1


def _truncar(texto: str | None, limite: int = LIMITE_CHAR_CAMPO) -> str:
    """Trunca um texto preservando o início, adicionando reticências se cortar."""
    if not texto:
        return ""
    if len(texto) <= limite:
        return texto
    return texto[:limite].rstrip() + " …"


def _formatar_data(iso: str | None) -> str:
    """Converte 'YYYY-MM-DD' em 'DD/MM/YYYY' para exibição legível."""
    if not iso:
        return ""
    try:
        from datetime import datetime
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return iso


# ----------------------------------------------------------------------------
# Montagem do conteúdo das tarefas
# ----------------------------------------------------------------------------

def _tarefa_para_texto(t: dict[str, Any]) -> str:
    """Renderiza uma tarefa como bloco de texto para o prompt.

    Destaca os 3 campos de conteúdo (Nome, Checklist, Notas) e adiciona
    contexto (status, responsáveis, categoria, datas).
    """
    partes: list[str] = []

    # Cabeçalho com contexto
    status = t.get("status") or "—"
    categoria = t.get("categoria") or "—"
    pessoas = ", ".join(t.get("responsaveis") or []) or "—"
    data_ref = _formatar_data(t.get("data_conclusao"))
    cabecalho = f"- [{status}] "
    if data_ref:
        cabecalho += f"({data_ref}) "
    cabecalho += f"[{categoria}] {pessoas}"
    partes.append(cabecalho)

    # Nome da tarefa (conteúdo principal)
    nome = _truncar(t.get("nome"), LIMITE_CHAR_CAMPO)
    if nome:
        partes.append(f"    Tarefa: {nome}")

    # Checklist (conteúdo)
    checklist = _truncar(t.get("checklist_itens"), LIMITE_CHAR_CAMPO)
    if checklist:
        concluidos = t.get("checklist_concluidos")
        sufixo = f" (concluídos: {concluidos})" if concluidos else ""
        partes.append(f"    Checklist{sufixo}: {checklist}")

    # Notas (conteúdo)
    notas = _truncar(t.get("notas"), LIMITE_CHAR_CAMPO)
    if notas:
        partes.append(f"    Notas: {notas}")

    return "\n".join(partes)


def _contexto_filtros(filtros: dict[str, Any]) -> str:
    """Descreve textualmente os filtros aplicados, para o prompt."""
    linhas: list[str] = []

    periodo = filtros.get("periodo_descricao")
    if periodo:
        linhas.append(f"- Período analisado: {periodo}")

    pessoas = filtros.get("pessoas")
    if pessoas:
        linhas.append(f"- Pessoas: {', '.join(pessoas)}")
    else:
        linhas.append("- Pessoas: todas")

    categorias = filtros.get("categorias")
    if categorias:
        linhas.append(f"- Categorias: {', '.join(categorias)}")

    status = filtros.get("status")
    if status:
        linhas.append(f"- Status: {', '.join(status)}")

    rotulos = filtros.get("rotulos")
    if rotulos:
        linhas.append(f"- Rótulos: {', '.join(rotulos)}")

    return "\n".join(linhas)


def _prompt_sistema(tom: str, pessoa: str, contexto: dict[str, Any]) -> str:
    """Define o papel, o tom e a pessoa gramatical do modelo.

    Decide singular/plural com base no número de pessoas envolvidas nas tarefas
    filtradas (contexto['pessoas_envolvidas']).
    """
    base = (
        "Você é um analista que resume atividades de trabalho a partir dos dados de "
        "tarefas exportadas do Microsoft Planner. Escreva sempre em português do Brasil "
        "(pt-BR). Entregue APENAS o texto final do resumo, sem descrever seu processo "
        "de raciocínio. "
        "NÃO use formatação Markdown (sem negrito, itálico, #). "
        "BASEIE-SE EXCLUSIVAMENTE nas tarefas fornecidas no prompt: não invente, não "
        "infira e não inclua atividades que não estejam presentes nos dados, mesmo que "
        "pareçam coerentes com o contexto. Se uma informação não constar nas tarefas, "
        "simplesmente não a mencione. "
        "IMPORTANTE: uma palavra citada de passagem (ex.: em uma URL ou nota breve) NÃO "
        "significa que a atividade relacionada foi realizada — só descreva o que a tarefa "
        "efetivamente registra. Não faça deduções nem complete lacunas; resuma apenas o "
        "que está explicitamente escrito nos campos fornecidos."
    )

    # --- Pessoa gramatical + número (singular/plural) ---------------------
    envolvidas = contexto.get("pessoas_envolvidas") or []
    nomes = [n for n in envolvidas if n]
    singular = len(nomes) == 1
    nome_unico = nomes[0] if singular else None

    if pessoa == "primeira":
        if singular:
            pessoa_instr = (
                f" Escreva em PRIMEIRA PESSOA DO SINGULAR, como se você fosse {nome_unico} "
                "prestando contas das próprias atividades (use 'eu', 'realizei', 'concluí')."
            )
        else:
            pessoas_lista = ", ".join(nomes) if nomes else "a equipe"
            pessoa_instr = (
                " Escreva em PRIMEIRA PESSOA DO PLURAL, como se fosse a equipe ('nós', "
                f"'realizamos', 'concluímos') composta por: {pessoas_lista}."
            )
    else:  # terceira pessoa
        if singular:
            pessoa_instr = (
                f" Escreva em TERCEIRA PESSOA DO SINGULAR, referindo-se a {nome_unico} "
                "(use o nome ou 'ele/ela', 'realizou', 'concluíu')."
            )
        elif nomes:
            pessoas_lista = ", ".join(nomes)
            pessoa_instr = (
                f" Escreva em TERCEIRA PESSOA DO PLURAL, referindo-se à equipe composta "
                f"por: {pessoas_lista} (use 'a equipe' ou os nomes, 'realizaram', "
                "'concluíram')."
            )
        else:
            pessoa_instr = " Escreva em terceira pessoa, referindo-se à equipe."

    # --- Tom ---------------------------------------------------------------
    if tom == "narrativo":
        tom_instr = (
            " Use um tom narrativo e descritivo, em prosa fluida, contando o que foi "
            "feito no período como uma narrativa coesa. Agrupe por temas quando fizer "
            "sentido."
        )
    else:  # executivo/profissional
        tom_instr = (
            " Use um tom executivo e profissional, voltado a prestação de contas e "
            "relatório de produtividade. Destaque o que foi realizado, agrupado por "
            "temas quando relevante."
        )

    return base + tom_instr + pessoa_instr


def _prompt_usuario(
    tarefas: list[dict[str, Any]],
    filtros: dict[str, Any],
    tamanho_palavras: int,
    parcial: bool = False,
    resumos_anteriores: list[str] | None = None,
) -> str:
    """Monta o prompt do usuário.

    Args:
        tarefas: lista de tarefas (já filtradas) a resumir.
        filtros: descrição dos filtros aplicados.
        tamanho_palavras: número-alvo de palavras do resumo.
        parcial: se True, é um resumo de lote (map step); usa alvo proporcional menor.
        resumos_anteriores: se fornecido, é a etapa reduce (consolidar resumos parciais).
    """
    contexto = _contexto_filtros(filtros)

    if resumos_anteriores:
        # Etapa REDUCE: consolidar resumos parciais em um texto final coeso.
        blocos = "\n\n".join(f"--- Parte {i + 1} ---\n{r}" for i, r in enumerate(resumos_anteriores))
        return (
            f"Contexto dos filtros aplicados:\n{contexto}\n\n"
            f"Abaixo estão {len(resumos_anteriores)} resumos parciais das atividades do "
            f"período, cada um cobrindo um subconjunto de tarefas. Consolide tudo em um "
            f"ÚNICO resumo coeso, sem repetições, agrupando por tema. Resuma o CONTEÚDO "
            f"das atividades (o que foi feito), não apenas contagens. "
            f"Escreva aproximadamente {tamanho_palavras} palavras.\n\n"
            f"{blocos}\n\n"
            f"Resumo consolidado:"
        )

    # Etapa MAP (lote) ou chamada única
    blocos_tarefas = "\n".join(_tarefa_para_texto(t) for t in tarefas)
    alvo = max(150, tamanho_palavras // 3) if parcial else tamanho_palavras
    periodo = filtros.get("periodo_descricao", "todo o período")

    instrucao = (
        f"Resuma o CONTEÚDO das {len(tarefas)} tarefas abaixo — todas concluídas/registradas "
        f"no período de {periodo}. Resuma APENAS o que está descrito nestas tarefas; "
        f"qualquer atividade fora deste período ou não listada NÃO deve aparecer no resumo. "
        f"Sintetize com base nos campos 'Tarefa' (nome), 'Checklist' e 'Notas'. Agrupe "
        f"atividades relacionadas por tema. NÃO faça deduções nem complete com conhecimentos "
        f"externos. "
    )
    if parcial:
        instrucao += (
            f"Este é um resumo PARCIAL de um subconjunto; seja objetivo, em aproximadamente "
            f"{alvo} palavras. "
        )
    else:
        instrucao += f"Escreva aproximadamente {alvo} palavras. "

    return (
        f"Contexto dos filtros aplicados:\n{contexto}\n\n"
        f"{instrucao}\n\n"
        f"Tarefas:\n{blocos_tarefas}\n\n"
        f"Resumo:"
    )


# ----------------------------------------------------------------------------
# Chamada à API (com fallback entre provedores/modelos)
# ----------------------------------------------------------------------------

def _chamar_modelo(
    endpoint: str,
    modelo: str,
    chave: str,
    mensagens: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str | None, str | None]:
    """Faz uma chamada a um modelo. Retorna (texto, erro)."""
    payload: dict[str, Any] = {
        "model": modelo,
        "messages": mensagens,
        "temperature": 0.4,
        "stream": False,
    }
    # Gemini 2.5 Flash é um modelo "thinking": max_tokens limita o TOTAL (raciocínio +
    # resposta visível), causando truncamento prematuro. Por isso não enviamos max_tokens
    # para o Gemini — deixamos o modelo usar o budget padrão.
    # Para os demais provedores, max_tokens controla apenas a resposta final.
    if "googleapis.com" not in endpoint:
        payload["max_tokens"] = max_tokens
    # ZAI (GLM-4.5+) e DeepSeek V4 ativam "thinking" por padrão, fazendo o modelo
    # escrever seu raciocínio interno no content ou consumir tokens com reasoning_content.
    # Desabilitamos para obter apenas a resposta final direta no content.
    # (Gemini usa outro formato e não aceita este parâmetro — é tratado via max_tokens.)
    if "deepseek.com" in endpoint or "z.ai" in endpoint or "bigmodel.cn" in endpoint:
        payload["thinking"] = {"type": "disabled"}
    headers = {
        "Authorization": f"Bearer {chave}",
        "Content-Type": "application/json",
    }
    try:
        resp = _session.post(endpoint, json=payload, headers=headers, timeout=TIMEOUT_SEGUNDOS)
    except requests.RequestException as exc:
        return None, f"erro de rede: {exc}"

    if resp.status_code != 200:
        # Extrai mensagem de erro quando possível (limite, modelo inválido, auth).
        trecho = ""
        try:
            trecho = resp.json().get("error", {}).get("message", "") or resp.text[:200]
        except Exception:
            trecho = resp.text[:200]
        return None, f"HTTP {resp.status_code}: {trecho}"

    try:
        dados = resp.json()
        escolhas = dados.get("choices", [])
        if not escolhas:
            return None, "resposta sem 'choices'"
        texto = escolhas[0].get("message", {}).get("content", "").strip()
        if not texto:
            # Alguns provedores (DeepSeek reasoner) podem trazer reasoning_content.
            texto = escolhas[0].get("message", {}).get("reasoning_content", "").strip()
        if not texto:
            return None, "resposta vazia"
        return texto, None
    except Exception as exc:
        return None, f"erro ao decodificar JSON: {exc}"


def _tentar_modelos(
    mensagens: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Tenta os modelos em ordem até um funcionar.

    Returns:
        (texto, provedor_usado, modelo_usado, erro_final) — texto/provedor/modelo
        são None se todos falharem; erro_final descreve o último problema.
    """
    ultimo_erro: str | None = None
    for provedor, endpoint, modelo, env_var, _limite in PROVEDORES:
        chave = os.getenv(env_var, "").strip()
        if not chave:
            logger.info("Chave %s ausente; pulando %s/%s", env_var, provedor, modelo)
            continue
        logger.info("Tentando %s/%s ...", provedor, modelo)
        texto, erro = _chamar_modelo(endpoint, modelo, chave, mensagens, max_tokens)
        if texto:
            logger.info("Sucesso com %s/%s", provedor, modelo)
            return texto, provedor, modelo, None
        logger.warning("Falhou %s/%s: %s", provedor, modelo, erro)
        ultimo_erro = f"{provedor}/{modelo}: {erro}"
    return None, None, None, ultimo_erro


# ----------------------------------------------------------------------------
# Orquestração do resumo (com map-reduce para volumes grandes)
# ----------------------------------------------------------------------------

def gerar_resumo(
    tarefas: list[dict[str, Any]],
    filtros: dict[str, Any],
    tamanho: str = "curto",
    tom: str = "executivo",
    pessoa: str = "terceira",
) -> dict[str, Any]:
    """Gera o resumo de conteúdo das tarefas filtradas.

    Args:
        tarefas: tarefas já filtradas (lista de dicts no formato de planner.carregar_dados).
        filtros: dict com chaves opcionais {pessoas, categorias, status, rotulos,
                 periodo_descricao, pessoas_envolvidas}.
        tamanho: 'curto' | 'medio' | 'longo' | 'muito_longo'.
        tom: 'executivo' | 'narrativo'.
        pessoa: 'terceira' | 'primeira' — pessoa gramatical do texto final.

    Returns:
        {texto, provedor, modelo, tarefas_consideradas, erro}
    """
    tamanho_palavras = TAMANHOS.get(tamanho, TAMANHOS["curto"])
    max_tokens_saida = int(tamanho_palavras * TOKENS_POR_PALAVRA) + 200

    if not tarefas:
        return {
            "texto": None,
            "provedor": None,
            "modelo": None,
            "tarefas_consideradas": 0,
            "erro": "Nenhuma tarefa corresponde aos filtros selecionados para gerar o resumo.",
        }

    # Filtra tarefas que tenham pelo menos algum conteúdo (nome, checklist ou notas).
    tarefas_com_conteudo = [
        t for t in tarefas
        if _limpar(t.get("nome")) or _limpar(t.get("checklist_itens")) or _limpar(t.get("notas"))
    ]
    if not tarefas_com_conteudo:
        return {
            "texto": None,
            "provedor": None,
            "modelo": None,
            "tarefas_consideradas": len(tarefas),
            "erro": "As tarefas filtradas não possuem conteúdo (nome, checklist ou notas) para resumir.",
        }

    sistema = _prompt_sistema(tom, pessoa, filtros)
    conteudo_total = "\n".join(_tarefa_para_texto(t) for t in tarefas_com_conteudo)
    estimativa_tokens = _estimar_tokens(conteudo_total) + max_tokens_saida + 1000

    # Decide entre chamada única e map-reduce com base no limite do MENOR provedor.
    limite_minimo = min(p[4] for p in PROVEDORES)
    usar_map_reduce = estimativa_tokens > (limite_minimo * 0.8) and len(tarefas_com_conteudo) > TAMANHO_LOTE

    if not usar_map_reduce:
        # Chamada única.
        mensagens = [
            {"role": "system", "content": sistema},
            {"role": "user", "content": _prompt_usuario(tarefas_com_conteudo, filtros, tamanho_palavras)},
        ]
        texto, provedor, modelo, erro = _tentar_modelos(mensagens, max_tokens_saida)
        return {
            "texto": texto,
            "provedor": provedor,
            "modelo": modelo,
            "tarefas_consideradas": len(tarefas_com_conteudo),
            "erro": erro,
        }

    # --- Map-reduce ---------------------------------------------------------
    logger.info(
        "Aplicando map-reduce: %d tarefas, ~%d tokens estimados.",
        len(tarefas_com_conteudo), estimativa_tokens,
    )
    lotes = [
        tarefas_com_conteudo[i:i + TAMANHO_LOTE]
        for i in range(0, len(tarefas_com_conteudo), TAMANHO_LOTE)
    ]
    resumos_parciais: list[str] = []
    provedor_usado = None
    modelo_usado = None

    for idx, lote in enumerate(lotes):
        mensagens = [
            {"role": "system", "content": sistema},
            {"role": "user", "content": _prompt_usuario(lote, filtros, tamanho_palavras, parcial=True)},
        ]
        # Em cada lote, max_tokens proporcional ao alvo parcial.
        alvo_lote = max(150, tamanho_palavras // 3)
        max_lote = int(alvo_lote * TOKENS_POR_PALAVRA) + 200
        texto, provedor, modelo, erro = _tentar_modelos(mensagens, max_lote)
        if texto:
            resumos_parciais.append(texto)
            if provedor_usado is None:
                provedor_usado, modelo_usado = provedor, modelo
        else:
            logger.warning("Lote %d falhou em todos os modelos: %s", idx + 1, erro)

    if not resumos_parciais:
        return {
            "texto": None,
            "provedor": None,
            "modelo": None,
            "tarefas_consideradas": len(tarefas_com_conteudo),
            "erro": "Não foi possível gerar o resumo: todos os modelos falharam nos lotes.",
        }

    # Etapa REDUCE: consolida os resumos parciais.
    mensagens_reduce = [
        {"role": "system", "content": sistema},
        {"role": "user", "content": _prompt_usuario(
            tarefas=[], filtros=filtros, tamanho_palavras=tamanho_palavras,
            resumos_anteriores=resumos_parciais,
        )},
    ]
    texto, provedor, modelo, erro = _tentar_modelos(mensagens_reduce, max_tokens_saida)
    return {
        "texto": texto,
        "provedor": provedor or provedor_usado,
        "modelo": modelo or modelo_usado,
        "tarefas_consideradas": len(tarefas_com_conteudo),
        "erro": erro,
    }


def _limpar(valor: Any) -> str:
    """Versão local de limpeza de texto (None/vazio -> '')."""
    if valor is None:
        return ""
    if isinstance(valor, float):
        try:
            import math
            if math.isnan(valor):
                return ""
        except Exception:
            pass
    s = str(valor).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    return s
