# Analisador de Planilhas do Microsoft Planner

Aplicação web local (Flask + HTML/CSS/JS) que carrega uma planilha exportada do
Microsoft Planner, valida a estrutura, monta um **dashboard filtrável** e gera
**resumos das atividades por IA** (sintetizando o conteúdo das tarefas).

## Funcionalidades

- **Upload com validação:** verifica se o `.xlsx` tem a estrutura esperada (aba
  `Dados Consolidados` e colunas obrigatórias). Se inválido, orienta a exportar
  corretamente do Planner.
- **Dashboard** com KPIs, gráficos (Chart.js) e tabela detalhada:
  - Atividades por pessoa, distribuição por status/categoria, pontualidade,
    evolução temporal de conclusões.
- **Filtros** (afetam gráficos, tabela e resumo):
  - Pessoas, Categoria, Status, Rótulos (seleção múltipla).
  - **Data de conclusão (real)** — padrão: últimos 30 dias.
  - **Data de prazo** — padrão: tudo.
  - Ambos com presets (7/30/90 dias, este mês, tudo) ou intervalo personalizado.
- **Resumo por IA:** gera um texto que sintetiza o **conteúdo** das tarefas
  filtradas (campos `Nome da tarefa`, `Itens da lista de verificação` e `Notas`),
  com tamanho e tom ajustáveis. Tenta os provedores em ordem — Gemini
  (gemini-3.5-flash, gemini-2.5-flash), depois ZAI (glm-4.5-flash) e por fim
  DeepSeek (deepseek-v4-flash, deepseek-chat) — parando no primeiro que responder.

## Pré-requisitos

- **Docker** (recomendado): Docker + Docker Compose.
- **Ou Python local**: Python 3.10+ (testado com 3.13). As dependências
  (incluindo pandas e openpyxl) são instaladas via `requirements.txt`.

## Instalação e execução

### Com Docker Compose (recomendado)

```bash
# 1. copiar o template e preencher as chaves de API
cp .env.example .env
#    ZAI_API_KEY=...
#    DEEPSEEK_API_KEY=...
#    GEMINI_API_KEY=...

# 2. subir o container
docker compose up -d --build
```

Depois abra **http://localhost:5000** no navegador. Comandos úteis:

```bash
docker compose logs -f        # acompanhar logs
docker compose down           # parar
docker compose up -d --build  # reconstruir após alterar código/dependências
```

### Com Python local

```bash
# 1. (opcional) criar e ativar um ambiente virtual
python -m venv .venv
.venv\Scripts\activate        # Windows (Git Bash: source .venv/Scripts/activate)

# 2. instalar dependências
pip install -r requirements.txt

# 3. copiar o template e preencher as chaves de API (cp .env.example .env)

# 4. iniciar o servidor
python app.py
```

Depois abra **http://localhost:5000** no navegador.

## Como exportar a planilha do Planner

1. Abra o plano no **Microsoft Planner**.
2. No menu do plano (reticências), escolha **"Exportar para Excel"**.
3. O arquivo gerado terá as abas: _Plano_, _Dados Consolidados_, _Tarefas_,
   _Metas_, _Buckets_ e _Usuários_.
4. Envie esse arquivo na aplicação sem alterar a estrutura.

## Estrutura do projeto

```
app.py              Servidor Flask (rotas /api/upload, /api/dados, /api/resumo)
planner.py          Leitura e validação do Excel
llm.py              Geração de resumo multi-modelo (Gemini → ZAI → DeepSeek)
templates/          HTML da página única
static/css/         Folha de estilos
static/js/          Lógica do frontend (filtros, gráficos, resumo)
requirements.txt    Dependências Python (Flask, pandas, openpyxl, requests, python-dotenv)
Dockerfile          Imagem Docker (python:3.13-slim)
docker-compose.yml  Orquestração do container (porta 5000, env_file=.env)
.env                Chaves de API (ZAI_API_KEY, DEEPSEEK_API_KEY, GEMINI_API_KEY)
.env.example        Template das variáveis de ambiente (sem chaves)
```

## Observações

- A aplicação é de uso local; rode com `python app.py` (bind `127.0.0.1:5000`) ou
  via `docker compose up` (acessível em `http://localhost:5000`).
- O arquivo enviado fica em diretório temporário durante a sessão; nada é
  persistido em banco de dados.
- Para volumes grandes de conteúdo, o resumo usa estratégia de **map-reduce**
  (resume lotes de tarefas e depois consolida) para respeitar o limite de
  contexto dos modelos sem perder conteúdo.
