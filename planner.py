"""Leitura e validação de planilhas exportadas do Microsoft Planner.

A planilha de referência (planner-example.xlsx) possui as abas:
    Plano, Dados Consolidados, Tarefas, Metas, Buckets, Usuários

A aba principal é "Dados Consolidados" (nomes resolvidos), com 19 colunas.
Datas vêm como texto ISO (YYYY-MM-DD).

Semântica das datas (conforme ajuste solicitado):
  - "Data de conclusão"  -> tratada como DATA REAL de conclusão.
  - "Concluído em"       -> fallback: usado quando "Data de conclusão" está vazio.
  - O campo computado `data_conclusao` é o que alimenta os filtros/gráficos.
"""
from __future__ import annotations

import os
from datetime import datetime, date
from typing import Any

import pandas as pd

# Aba principal de tarefas (com nomes resolvidos em vez de IDs brutos).
ABA_PRINCIPAL = "Dados Consolidados"

# Abas esperadas no arquivo exportado pelo Planner.
ABAS_ESPERADAS = ["Plano", "Dados Consolidados", "Tarefas", "Metas", "Buckets", "Usuários"]

# Colunas obrigatórias na aba principal (correspondência tolerante a caixa/espaço).
# Conteúdo -> usado no resumo por IA; Classificação -> filtros; Datas -> filtros de período.
COLUNAS_OBRIGATORIAS = [
    # Conteúdo (essencial ao resumo)
    "Nome da tarefa",
    "Itens da lista de verificação",
    "Notas",
    # Classificação (filtros)
    "Categoria",
    "Status",
    "Atribuído a",
    "Rótulos",
    "Prioridade",
    # Datas (filtros de período)
    "Criado em",
    "Data de conclusão",   # data real de conclusão
    "Concluído em",        # fallback quando "Data de conclusão" está vazio
    # Outros
    "Concluída por",
]


def _normalizar_nome(texto: str) -> str:
    """Normaliza um nome de coluna para comparação tolerante.

    Lowercase, sem acentos e sem espaços extras, para casar variações como
    "Data da exportação " (com espaço) ou diferenças de caixa.
    """
    import unicodedata
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return " ".join(sem_acento.lower().split())


def _limpar_texto(valor: Any) -> str | None:
    """Converte um valor de célula em texto limpo (ou None se vazio)."""
    if valor is None:
        return None
    if isinstance(valor, float) and pd.isna(valor):
        return None
    texto = str(valor).strip()
    if texto == "" or texto.lower() in ("nan", "none", "nat"):
        return None
    return texto


def _parse_data(valor: Any) -> str | None:
    """Converte um valor (texto ISO, datetime ou serial do Excel) em 'YYYY-MM-DD'.

    Retorna None se vazio. Tenta primeiro o formato ISO (como vem no Planner),
    depois cai para o parser genérico do pandas.
    """
    if valor is None:
        return None
    if isinstance(valor, float) and pd.isna(valor):
        return None
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%Y-%m-%d")
    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "none", "nat"):
        return None
    # Tenta ISO (formato padrão do Planner).
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(texto, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback genérico (lida com seriais do Excel quando aplicável).
    try:
        return pd.to_datetime(texto, errors="raise").strftime("%Y-%m-%d")
    except Exception:
        return None


def _split_pessoas(valor: Any) -> list[str]:
    """Quebra uma célula multi-valorada de pessoas (separadas por ';') em lista."""
    texto = _limpar_texto(valor)
    if not texto:
        return []
    return [p.strip() for p in texto.split(";") if p.strip()]


def ler_abas(caminho: str) -> dict[str, pd.DataFrame]:
    """Lê todas as abas do arquivo Excel em um dict {nome_aba: DataFrame}.

    Usa o cabeçalho da primeira linha. Abas vazias viram DataFrames vazios.
    """
    return pd.read_excel(caminho, sheet_name=None, dtype=str)


def validar_planilha(caminho: str) -> tuple[bool, list[str], list[str]]:
    """Valida a estrutura do arquivo Excel do Planner.

    Returns:
        (ok, erros, avisos):
        - ok: True se a aba principal existe e tem todas as colunas obrigatórias.
        - erros: mensagens que bloqueiam o uso.
        - avisos: mensagens informativas (ex.: abas opcionais ausentes).
    """
    erros: list[str] = []
    avisos: list[str] = []

    if not os.path.exists(caminho):
        return False, [f"Arquivo não encontrado: {caminho}"], []

    try:
        abas = ler_abas(caminho)
    except Exception as exc:  # arquivo corrompido ou não é Excel válido
        return False, [
            "Não foi possível ler o arquivo. Verifique se é um Excel (.xlsx) válido "
            f"exportado pelo Microsoft Planner. Detalhe: {exc}"
        ], []

    nomes_abas = list(abas.keys())

    # 1) Aba principal é mandatória.
    if ABA_PRINCIPAL not in nomes_abas:
        # Tenta correspondência tolerante.
        correspondencia = {
            _normalizar_nome(n): n for n in nomes_abas
        }
        if _normalizar_nome(ABA_PRINCIPAL) not in correspondencia:
            erros.append(
                "A aba principal 'Dados Consolidados' não foi encontrada. "
                "Exporte o plano pelo Microsoft Planner usando a opção "
                "'Exportar para Excel', que gera as abas padrão "
                "(Plano, Dados Consolidados, Tarefas, Buckets, Usuários, etc.)."
            )
            return False, erros, avisos

    df = abas[ABA_PRINCIPAL]

    # 2) Colunas obrigatórias (com correspondência tolerante).
    colunas_norm = {_normalizar_nome(c): c for c in df.columns}
    faltando = []
    for col in COLUNAS_OBRIGATORIAS:
        if _normalizar_nome(col) not in colunas_norm:
            faltando.append(col)
    if faltando:
        erros.append(
            "A aba 'Dados Consolidados' não possui todas as colunas esperadas. "
            f"Colunas ausentes: {', '.join(faltando)}. "
            "Isso indica que o arquivo não foi exportado corretamente pelo Planner. "
            "Reexporte o plano mantendo a estrutura padrão."
        )

    # 3) Avisos de abas opcionais ausentes.
    for aba in ABAS_ESPERADAS:
        if aba == ABA_PRINCIPAL:
            continue
        norm = _normalizar_nome(aba)
        if norm not in {_normalizar_nome(n) for n in nomes_abas}:
            avisos.append(f"Aba '{aba}' não encontrada (opcional; alguns detalhes podem ficar indisponíveis).")

    # 4) Pelo menos uma linha de dados.
    if len(df) == 0 and not erros:
        avisos.append("A aba 'Dados Consolidados' não contém tarefas (apenas cabeçalho).")

    ok = len(erros) == 0
    return ok, erros, avisos


def carregar_dados(caminho: str) -> dict[str, Any]:
    """Carrega e normaliza os dados do Excel do Planner.

    Returns:
        Dict com:
          - plano: metadados (nome, id, data de exportação)
          - tarefas: lista de tarefas normalizadas
          - buckets: lista de {id, nome}
          - usuarios: lista de {id, nome, email}
          - lookups: {pessoas, categorias, status, rotulos, prioridades} para filtros
    """
    abas = ler_abas(caminho)

    # --- Metadados do plano -------------------------------------------------
    plano = {"nome": None, "id": None, "exportacao": None}
    if "Plano" in abas and not abas["Plano"].empty:
        dfp = abas["Plano"]
        # Tolerante a nomes de coluna com espaços finais ("Data da exportação ").
        norm = {_normalizar_nome(c): c for c in dfp.columns}
        def _get(chave_norm: str) -> str | None:
            if chave_norm in norm:
                return _limpar_texto(dfp.iloc[0][norm[chave_norm]])
            return None
        plano["nome"] = _get("nome do plano")
        plano["id"] = _get("id do plano")
        plano["exportacao"] = _parse_data(_get("data da exportacao"))

    # --- Tarefas (aba principal) -------------------------------------------
    df = abas[ABA_PRINCIPAL]
    # Mapeia nomes normalizados -> nomes reais para acesso tolerante.
    col_norm = {_normalizar_nome(c): c for c in df.columns}

    def _col(nome: str):
        """Retorna a Series de uma coluna (ou Series de None se ausente)."""
        real = col_norm.get(_normalizar_nome(nome))
        if real is None:
            return pd.Series([None] * len(df))
        return df[real]

    tarefas: list[dict[str, Any]] = []
    for i in range(len(df)):
        atribuidos = _split_pessoas(_col("Atribuído a").iloc[i])
        concluida_por = _limpar_texto(_col("Concluída por").iloc[i])
        criado_por = _limpar_texto(_col("Criado por").iloc[i])

        # Datas: "Data de conclusão" é tratada como data REAL de conclusão;
        # quando está vazia, usamos "Concluído em" como fallback.
        data_conclusao = _parse_data(_col("Data de conclusão").iloc[i])
        if not data_conclusao:
            data_conclusao = _parse_data(_col("Concluído em").iloc[i])

        # Responsáveis: prioriza "Atribuído a"; se vazio, usa "Concluída por";
        # se também vazio, usa "Criado por".
        if atribuidos:
            responsaveis = atribuidos
        elif concluida_por:
            responsaveis = [concluida_por]
        elif criado_por:
            responsaveis = [criado_por]
        else:
            responsaveis = []

        tarefas.append({
            "id": _limpar_texto(_col("Identificação da tarefa").iloc[i]),
            # Conteúdo (preservado intacto para o resumo)
            "nome": _limpar_texto(_col("Nome da tarefa").iloc[i]),
            "checklist_itens": _limpar_texto(_col("Itens da lista de verificação").iloc[i]),
            "checklist_concluidos": _limpar_texto(_col("Itens concluídos da lista de verificação").iloc[i]),
            "notas": _limpar_texto(_col("Notas").iloc[i]),
            # Classificação
            "categoria": _limpar_texto(_col("Categoria").iloc[i]),
            "status": _limpar_texto(_col("Status").iloc[i]),
            "prioridade": _limpar_texto(_col("Prioridade").iloc[i]),
            "rotulos": _limpar_texto(_col("Rótulos").iloc[i]),
            "atribuidos": atribuidos,            # lista (campo bruto "Atribuído a")
            "responsaveis": responsaveis,        # lista (computado: Atribuído a > Concluída por > Criado por)
            "criado_por": criado_por,
            "concluida_por": concluida_por,
            # Datas (ISO YYYY-MM-DD)
            "criado_em": _parse_data(_col("Criado em").iloc[i]),
            "data_conclusao": data_conclusao,    # computada: "Data de conclusão" c/ fallback "Concluído em"
            "data_prazo": _parse_data(_col("Data de conclusão").iloc[i]),  # bruto (mantido p/ referência)
            "concluido_em": _parse_data(_col("Concluído em").iloc[i]),     # bruto (fallback)
        })

    # --- Lookups (buckets / usuários) --------------------------------------
    buckets: list[dict[str, Any]] = []
    if "Buckets" in abas and not abas["Buckets"].empty:
        dfb = abas["Buckets"]
        norm = {_normalizar_nome(c): c for c in dfb.columns}
        for _, r in dfb.iterrows():
            bid = _limpar_texto(r[norm["id de bucket"]]) if "id de bucket" in norm else None
            bnome = _limpar_texto(r[norm["nome do bucket"]]) if "nome do bucket" in norm else None
            if bnome:
                buckets.append({"id": bid, "nome": bnome})

    usuarios: list[dict[str, Any]] = []
    if "Usuários" in abas and not abas["Usuários"].empty:
        dfu = abas["Usuários"]
        norm = {_normalizar_nome(c): c for c in dfu.columns}
        for _, r in dfu.iterrows():
            uid = _limpar_texto(r[norm["id do usuario"]]) if "id do usuario" in norm else None
            unome = _limpar_texto(r[norm["nome do usuario"]]) if "nome do usuario" in norm else None
            uemail = _limpar_texto(r[norm["email"]]) if "email" in norm else None
            if unome:
                usuarios.append({"id": uid, "nome": unome, "email": uemail})

    # --- Agregados para filtros --------------------------------------------
    pessoas_set: set[str] = set()
    categorias_set: set[str] = set()
    status_set: set[str] = set()
    rotulos_set: set[str] = set()
    prioridades_set: set[str] = set()

    for t in tarefas:
        # Pessoas para filtro = responsáveis computados (Atribuído a > Concluída por > Criado por)
        for p in t["responsaveis"]:
            if p:
                pessoas_set.add(p)
        if t.get("categoria"):
            categorias_set.add(t["categoria"])
        if t.get("status"):
            status_set.add(t["status"])
        if t.get("prioridade"):
            prioridades_set.add(t["prioridade"])
        # Rótulos pode ser multi-valorado (separado por ';' no Planner).
        if t.get("rotulos"):
            for rot in str(t["rotulos"]).split(";"):
                rot = rot.strip()
                if rot:
                    rotulos_set.add(rot)

    lookups = {
        "pessoas": sorted(pessoas_set),
        "categorias": sorted(categorias_set),
        "status": sorted(status_set),
        "rotulos": sorted(rotulos_set),
        "prioridades": sorted(prioridades_set),
    }

    return {
        "plano": plano,
        "tarefas": tarefas,
        "buckets": buckets,
        "usuarios": usuarios,
        "lookups": lookups,
    }
