/* ============================================================================
   Analisador de Planilhas do Planner — lógica do frontend
   - Upload + validação
   - Dashboard com filtros (multiselect chips + períodos)
   - KPIs, gráficos (Chart.js) e tabela
   - Resumo de conteúdo por IA
   ========================================================================== */
(() => {
  "use strict";

  // Estado global da aplicação.
  const estado = {
    dados: null,           // dados completos vindos da API
    filtros: {
      pessoas: new Set(),
      categorias: new Set(),
      status: new Set(),
      rotulos: new Set(),
      data_conclusao: { tipo: "preset", preset: "tudo" },
    },
    graficos: {},          // instâncias de Chart.js por id
    ordenacao: { coluna: "data_conclusao", dir: "desc" },
    busca: "",
  };

  // Paleta de cores consistente para gráficos.
  const CORES = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#8b5cf6", "#06b6d4", "#ec4899", "#64748b"];

  // ----------------------------------------------------------------- utils
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));
  const esconder = (el) => el && el.setAttribute("hidden", "");
  const mostrar = (el) => el && el.removeAttribute("hidden");

  function fmtData(iso) {
    if (!iso) return "—";
    const [y, m, d] = iso.split("-");
    if (!y || !m || !d) return iso;
    return `${d}/${m}/${y}`;
  }
  function escapar(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function classeStatus(status) {
    const s = (status || "").toLowerCase();
    if (s.includes("conclu")) return "concluida";
    if (s.includes("andamento")) return "andamento";
    return "nao-iniciado";
  }

  // ============================================================ ESTADO A: UPLOAD
  function configurarUpload() {
    const dropzone = $("#dropzone");
    const input = $("#input-arquivo");

    dropzone.addEventListener("click", () => input.click());
    dropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", (e) => {
      if (e.target.files[0]) enviarArquivo(e.target.files[0]);
    });

    ["dragenter", "dragover"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
    );
    dropzone.addEventListener("drop", (e) => {
      const f = e.dataTransfer.files[0];
      if (f) enviarArquivo(f);
    });

    $("#btn-trocar-arquivo").addEventListener("click", voltarParaUpload);
  }

  async function enviarArquivo(arquivo) {
    esconder($("#upload-erro")); esconder($("#upload-aviso"));
    mostrar($("#upload-status"));

    const fd = new FormData();
    fd.append("arquivo", arquivo);
    try {
      const resp = await fetch("/api/upload", { method: "POST", body: fd });
      const dados = await resp.json();
      esconder($("#upload-status"));

      if (!dados.ok) {
        const erroEl = $("#upload-erro");
        let msg = "<strong>Não foi possível carregar a planilha.</strong><br>";
        msg += (dados.erros || []).map(escapar).join("<br>");
        erroEl.innerHTML = msg;
        mostrar(erroEl);
        // Se houver avisos mesmo no erro, mostra-os também.
        if (dados.avisos && dados.avisos.length) {
          $("#upload-aviso").innerHTML = dados.avisos.map(escapar).join("<br>");
          mostrar($("#upload-aviso"));
        }
        return;
      }

      if (dados.avisos && dados.avisos.length) {
        $("#upload-aviso").innerHTML = dados.avisos.map(escapar).join("<br>");
        mostrar($("#upload-aviso"));
      }
      await carregarDashboard();
    } catch (err) {
      esconder($("#upload-status"));
      $("#upload-erro").textContent = "Erro de comunicação com o servidor: " + err.message;
      mostrar($("#upload-erro"));
    }
  }

  function voltarParaUpload() {
    mostrar($("#tela-upload"));
    esconder($("#tela-dashboard"));
    esconder($("#plano-info"));
    esconder($("#btn-trocar-arquivo"));
    $("#input-arquivo").value = "";
  }

  // ====================================================== CARREGAR DASHBOARD
  async function carregarDashboard() {
    try {
      const resp = await fetch("/api/dados");
      const payload = await resp.json();
      if (!payload.ok) {
        $("#upload-erro").textContent = payload.erro || "Erro ao carregar dados.";
        mostrar($("#upload-erro"));
        return;
      }
      estado.dados = payload.dados;

      // Header com nome do plano.
      const nome = estado.dados.plano?.nome;
      if (nome) $("#plano-nome").textContent = "📋 " + nome;

      mostrar($("#tela-dashboard"));
      esconder($("#tela-upload"));
      mostrar($("#plano-info"));
      mostrar($("#btn-trocar-arquivo"));

      construirFiltros();
      configurarPeriodos();
      $("#btn-limpar").addEventListener("click", limparFiltros);
      $("#busca-tabela").addEventListener("input", (e) => {
        estado.busca = e.target.value.toLowerCase();
        renderTabela();
      });
      configurarOrdenacao();
      configurarBtnTopo();
      configurarVisuais();

      // Resumo.
      $("#btn-gerar-resumo").addEventListener("click", gerarResumo);
      $("#btn-copiar-resumo").addEventListener("click", copiarResumo);

      atualizarTudo();
    } catch (err) {
      $("#upload-erro").textContent = "Erro ao carregar dashboard: " + err.message;
      mostrar($("#upload-erro"));
    }
  }

  // ============================================================ FILTROS
  // Valor sentinela para filtrar registros sem rótulo.
  const SEM_ROTULO = "__sem_rotulo__";

  function construirFiltros() {
    const lk = estado.dados.lookups;
    montarMultiselect("#filtro-pessoas", lk.pessoas, estado.filtros.pessoas, "(sem pessoa)");
    montarMultiselect("#filtro-categorias", lk.categorias, estado.filtros.categorias, "(sem categoria)");
    montarMultiselect("#filtro-status", lk.status, estado.filtros.status, "(sem status)");
    montarMultiselectRotulos("#filtro-rotulos", lk.rotulos, estado.filtros.rotulos, "(sem rótulo)");
  }

  function montarMultiselectRotulos(seletor, valores, setSelecionado, textoVazio) {
    const container = $(seletor);
    container.innerHTML = "";
    // Chip especial: "Sem rótulo" — filtra registros que NÃO possuem rótulo.
    const chipSem = document.createElement("span");
    chipSem.className = "chip";
    chipSem.textContent = "⊘ Sem rótulo";
    chipSem.dataset.valor = SEM_ROTULO;
    chipSem.addEventListener("click", () => {
      if (setSelecionado.has(SEM_ROTULO)) { setSelecionado.delete(SEM_ROTULO); chipSem.classList.remove("ativo"); }
      else { setSelecionado.add(SEM_ROTULO); chipSem.classList.add("ativo"); }
      atualizarTudo();
    });
    container.appendChild(chipSem);
    // Chips dos rótulos existentes.
    if (!valores || valores.length === 0) {
      return;
    }
    valores.forEach((v) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = v;
      chip.dataset.valor = v;
      chip.addEventListener("click", () => {
        if (setSelecionado.has(v)) { setSelecionado.delete(v); chip.classList.remove("ativo"); }
        else { setSelecionado.add(v); chip.classList.add("ativo"); }
        atualizarTudo();
      });
      container.appendChild(chip);
    });
  }

  function montarMultiselect(seletor, valores, setSelecionado, textoVazio) {
    const container = $(seletor);
    container.innerHTML = "";
    if (!valores || valores.length === 0) {
      container.innerHTML = `<span class="chip vazio">${textoVazio}</span>`;
      return;
    }
    valores.forEach((v) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = v;
      chip.dataset.valor = v;
      chip.addEventListener("click", () => {
        if (setSelecionado.has(v)) { setSelecionado.delete(v); chip.classList.remove("ativo"); }
        else { setSelecionado.add(v); chip.classList.add("ativo"); }
        atualizarTudo();
      });
      container.appendChild(chip);
    });
  }

  function configurarPeriodos() {
    const cfg = [
      { prefixo: "conclusao", chave: "data_conclusao" },
    ];
    cfg.forEach(({ prefixo, chave }) => {
      const select = $(`#${prefixo}-preset`);
      const rangeDiv = $(`#${prefixo}-range`);
      const iniInput = $(`#${prefixo}-ini`);
      const fimInput = $(`#${prefixo}-fim`);

      select.addEventListener("change", () => {
        if (select.value === "range") {
          mostrar(rangeDiv);
          estado.filtros[chave] = { tipo: "range", inicio: null, fim: null };
        } else {
          esconder(rangeDiv);
          estado.filtros[chave] = { tipo: "preset", preset: select.value };
          atualizarTudo();
        }
      });
      [iniInput, fimInput].forEach((inp) => {
        inp.addEventListener("change", () => {
          const f = estado.filtros[chave];
          if (f.tipo === "range") {
            // input[type=date] retorna ISO (YYYY-MM-DD) ou "" nativamente.
            f.inicio = iniInput.value || null;
            f.fim = fimInput.value || null;
            atualizarTudo();
          }
        });
      });
    });
  }

  function limparFiltros() {
    ["pessoas", "categorias", "status", "rotulos"].forEach((k) => {
      estado.filtros[k].clear();
      $$(`#filtro-${k === "rotulos" ? "rotulos" : k} .chip`).forEach((c) => c.classList.remove("ativo"));
    });
    // Reseta período para o padrão (data de conclusão = Tudo).
    $("#conclusao-preset").value = "tudo";
    esconder($("#conclusao-range"));
    $("#conclusao-ini").value = ""; $("#conclusao-fim").value = "";
    estado.filtros.data_conclusao = { tipo: "preset", preset: "tudo" };

    estado.busca = "";
    $("#busca-tabela").value = "";
    atualizarTudo();
  }

  // ============================================================ FILTRAGEM (cliente — espelha o backend)
  function resolverPeriodo(spec) {
    if (!spec) return [null, null];
    const hoje = new Date();
    const iso = (d) => d.toISOString().slice(0, 10);
    if (spec.tipo === "range") return [spec.inicio, spec.fim];
    if (spec.preset === "tudo") return [null, null];
    const dias = { "7": 7, "30": 30, "90": 90 }[spec.preset];
    if (dias) {
      const ini = new Date(hoje); ini.setDate(ini.getDate() - dias);
      return [iso(ini), iso(hoje)];
    }
    if (spec.preset === "mes") {
      const ini = new Date(hoje.getFullYear(), hoje.getMonth(), 1);
      // "Este mês" = mês-calendário inteiro (1º ao último dia), não até hoje,
      // para incluir tarefas com data de conclusão futura dentro do mês.
      const fim = new Date(hoje.getFullYear(), hoje.getMonth() + 1, 0);
      return [iso(ini), iso(fim)];
    }
    if (spec.preset === "mes_anterior") {
      const ini = new Date(hoje.getFullYear(), hoje.getMonth() - 1, 1);
      const fim = new Date(hoje.getFullYear(), hoje.getMonth(), 0);
      return [iso(ini), iso(fim)];
    }
    return [null, null];
  }
  function dataDentro(valor, ini, fim) {
    if (ini == null && fim == null) return true;
    if (!valor) return false;
    if (ini && valor < ini) return false;
    if (fim && valor > fim) return false;
    return true;
  }

  function tarefasFiltradas() {
    const f = estado.filtros;
    const [ci, cf] = resolverPeriodo(f.data_conclusao);
    const temPeriodo = ci != null || cf != null;

    return estado.dados.tarefas.filter((t) => {
      if (f.pessoas.size) {
        const pessoas = new Set([...(t.responsaveis || [])]);
        let achou = false;
        for (const p of f.pessoas) if (pessoas.has(p)) { achou = true; break; }
        if (!achou) return false;
      }
      if (f.categorias.size && !f.categorias.has(t.categoria)) return false;
      if (f.status.size && !f.status.has(t.status)) return false;
      if (f.rotulos.size) {
        const rotulos = new Set((t.rotulos || "").split(";").map((s) => s.trim()).filter(Boolean));
        const semRotuloSel = f.rotulos.has(SEM_ROTULO);
        const rotulosSel = new Set([...f.rotulos].filter((r) => r !== SEM_ROTULO));
        // Passa se: tem algum rótulo selecionado presente, OU "Sem rótulo" está
        // selecionado e a tarefa não tem nenhum rótulo.
        let achou = false;
        for (const r of rotulosSel) if (rotulos.has(r)) { achou = true; break; }
        if (!achou && (semRotuloSel && rotulos.size === 0)) achou = true;
        if (!achou) return false;
      }
      if (temPeriodo && !dataDentro(t.data_conclusao, ci, cf)) return false;
      return true;
    });
  }

  // ============================================================ ATUALIZAR TUDO
  function atualizarTudo() {
    const filtradas = tarefasFiltradas();
    $("#contador-tarefas").textContent = `${filtradas.length} tarefa(s) exibida(s) de ${estado.dados.tarefas.length}`;
    renderKPIs(filtradas);
    renderGraficos(filtradas);
    renderTabela(filtradas);
  }

  // ---- Botão flutuante: voltar ao topo ----
  function configurarBtnTopo() {
    const btn = $("#btn-topo");
    window.addEventListener("scroll", () => {
      if (window.scrollY > 300) mostrar(btn);
      else esconder(btn);
    }, { passive: true });
    btn.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  // ---- Personalização de visuais ----
  const VISUAIS = [
    { id: "pessoas",  label: "Atividades por pessoa",                    padrao: false },
    { id: "status",   label: "Distribuição por status",                   padrao: true  },
    { id: "categoria",label: "Distribuição por categoria",                padrao: true  },
    { id: "semanal",  label: "Volume de conclusões por semana",           padrao: true  },
    { id: "evolucao", label: "Evolução de conclusões ao longo do tempo",  padrao: false },
  ];

  function configurarVisuais() {
    // Inicializa estado de visibilidade com os padrões.
    if (!estado.visuais) {
      estado.visuais = {};
      VISUAIS.forEach((v) => { estado.visuais[v.id] = v.padrao; });
    }

    const painel = $("#painel-visuais .visuais-opcoes");
    painel.innerHTML = VISUAIS.map((v) => {
      const ativo = estado.visuais[v.id];
      return `<label class="visuais-opcao">
        <input type="checkbox" data-visual-id="${v.id}" ${ativo ? "checked" : ""} />
        ${v.label}
      </label>`;
    }).join("");

    // Atualiza visibilidade dos cards no DOM.
    VISUAIS.forEach((v) => {
      const card = $(`.card-grafico[data-visual="${v.id}"]`);
      if (card) {
        if (estado.visuais[v.id]) card.classList.remove("oculto");
        else card.classList.add("oculto");
      }
    });

    // Toggle do painel.
    const btn = $("#btn-personalizar");
    const panel = $("#painel-visuais");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (panel.hidden) mostrar(panel);
      else esconder(panel);
    });
    document.addEventListener("click", () => esconder(panel), { once: false });
    panel.addEventListener("click", (e) => e.stopPropagation());

    // Ao marcar/desmarcar, atualiza estado e mostra/oculta/esconde.
    painel.addEventListener("change", (e) => {
      const cb = e.target;
      const id = cb.dataset.visualId;
      estado.visuais[id] = cb.checked;
      const card = $(`.card-grafico[data-visual="${id}"]`);
      if (card) {
        if (cb.checked) {
          card.classList.remove("oculto");
          // Re-renderiza só este gráfico
          const filtradas = tarefasFiltradas();
          renderGraficoUnico(id, filtradas);
        } else {
          card.classList.add("oculto");
          // Destroi o gráfico para liberar recurso
          if (estado.graficos[id]) {
            estado.graficos[id].destroy();
            delete estado.graficos[id];
          }
        }
      }
    });
  }

  function renderGraficoUnico(id, tarefas) {
    // Renderiza apenas um gráfico específico (útil quando o toggle re-exibe).
    if (estado.graficos[id]) estado.graficos[id].destroy();
    switch (id) {
      case "pessoas":   renderGraficoPessoas(tarefas); break;
      case "status":    renderGraficoStatus(tarefas); break;
      case "categoria": renderGraficoCategoria(tarefas); break;
      case "semanal":   renderSemanal(tarefas); break;
      case "evolucao":  renderEvolucao(tarefas); break;
    }
  }

  // ============================================================ KPIs
  function renderKPIs(tarefas) {
    const total = tarefas.length;
    const concluidas = tarefas.filter((t) => (t.status || "").toLowerCase().includes("conclu")).length;
    const andamento = tarefas.filter((t) => (t.status || "").toLowerCase().includes("andamento")).length;
    const naoIniciado = tarefas.filter((t) => (t.status || "").toLowerCase().includes("não") || (t.status || "").toLowerCase().includes("iniciado")).length;
    const pctConcluida = total ? Math.round((concluidas / total) * 100) : 0;
    // Pessoas distintas nas tarefas filtradas (atribuídos).
    const pessoas = new Set();
    tarefas.forEach((t) => (t.responsaveis || []).forEach((p) => pessoas.add(p)));
    const nPessoas = pessoas.size;

    const cards = [
      { rotulo: "Total de tarefas", valor: total },
      { rotulo: "Concluídas", valor: concluidas, classe: "sucesso" },
      { rotulo: "Em andamento", valor: andamento },
      { rotulo: "Não iniciadas", valor: naoIniciado },
      { rotulo: "% concluídas", valor: pctConcluida + "%", classe: "sucesso" },
      { rotulo: "Pessoas envolvidas", valor: nPessoas },
    ];
    $("#kpis").innerHTML = cards.map((c) =>
      `<div class="kpi ${c.classe || ""}"><div class="kpi-valor">${c.valor}</div><div class="kpi-rotulo">${c.rotulo}</div></div>`
    ).join("");
  }

  // ============================================================ GRÁFICOS
  function destruirGraficos() {
    Object.values(estado.graficos).forEach((g) => g && g.destroy());
    estado.graficos = {};
  }

  function renderGraficos(tarefas) {
    destruirGraficos();

    // Renderiza apenas os visuais que estão ativos.
    if (estado.visuais.pessoas)  renderGraficoPessoas(tarefas);
    if (estado.visuais.status)   renderGraficoStatus(tarefas);
    if (estado.visuais.categoria)renderGraficoCategoria(tarefas);
    if (estado.visuais.semanal)  renderSemanal(tarefas);
    if (estado.visuais.evolucao) renderEvolucao(tarefas);
  }

  function renderGraficoPessoas(tarefas) {
    const porPessoa = {};
    tarefas.forEach((t) => {
      const conclui = (t.status || "").toLowerCase().includes("conclu");
      (t.responsaveis || []).forEach((p) => {
        porPessoa[p] = porPessoa[p] || { total: 0, concluidas: 0 };
        porPessoa[p].total++;
        if (conclui) porPessoa[p].concluidas++;
      });
    });
    const pessoas = Object.keys(porPessoa).sort((a, b) => porPessoa[b].total - porPessoa[a].total);
    estado.graficos.pessoas = new Chart($("#grafico-pessoas"), {
      type: "bar",
      data: {
        labels: pessoas,
        datasets: [
          { label: "Total", data: pessoas.map((p) => porPessoa[p].total), backgroundColor: "#93c5fd" },
          { label: "Concluídas", data: pessoas.map((p) => porPessoa[p].concluidas), backgroundColor: "#2563eb" },
        ],
      },
      options: baseOpts(true, "pessoas"),
    });
  }

  function renderGraficoStatus(tarefas) {
    const porStatus = contar(tarefas, "status");
    estado.graficos.status = new Chart($("#grafico-status"), {
      type: "doughnut",
      data: { labels: Object.keys(porStatus), datasets: [{ data: Object.values(porStatus), backgroundColor: CORES }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } },
    });
  }

  function renderGraficoCategoria(tarefas) {
    const porCat = contar(tarefas, "categoria");
    const cats = Object.keys(porCat).sort((a, b) => porCat[b] - porCat[a]);
    estado.graficos.categoria = new Chart($("#grafico-categoria"), {
      type: "bar",
      data: { labels: cats, datasets: [{ label: "Tarefas", data: cats.map((c) => porCat[c]), backgroundColor: "#8b5cf6" }] },
      options: { ...baseOpts(true, "categoria"), indexAxis: "y" },
    });
  }

  function renderSemanal(tarefas) {
    // Agrupa conclusões por semana ISO (segunda-feira como início), considerando
    // apenas tarefas com data_conclusao preenchida.
    const porSemana = {};
    const semanasSet = new Set();
    tarefas.forEach((t) => {
      if (!t.data_conclusao) return;
      const semana = inicioSemanaISO(t.data_conclusao); // "YYYY-MM-DD" (segunda)
      semanasSet.add(semana);
      porSemana[semana] = (porSemana[semana] || 0) + 1;
    });
    const semanas = Array.from(semanasSet).sort();
    const labels = semanas.map((s) => {
      const [y, m, d] = s.split("-");
      return `${d}/${m}`;
    });
    estado.graficos.semanal = new Chart($("#grafico-semanal"), {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "Tarefas concluídas",
          data: semanas.map((s) => porSemana[s] || 0),
          backgroundColor: "#2563eb",
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { title: { display: true, text: "Semana (início)" } }, y: { beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }

  function inicioSemanaISO(isoDate) {
    // Recebe "YYYY-MM-DD" e retorna a segunda-feira daquela semana em "YYYY-MM-DD".
    const d = new Date(isoDate + "T00:00:00");
    const dia = d.getDay(); // 0=dom, 1=seg...
    const diff = dia === 0 ? -6 : 1 - dia;
    d.setDate(d.getDate() + diff);
    return d.toISOString().slice(0, 10);
  }

  function renderEvolucao(tarefas) {
    // Agrupa por mês (YYYY-MM) e por pessoa, usando data_conclusao (computada).
    const porMesPessoa = {};
    const mesesSet = new Set();
    tarefas.forEach((t) => {
      if (!t.data_conclusao) return;
      const mes = t.data_conclusao.slice(0, 7);
      mesesSet.add(mes);
      (t.responsaveis || []).forEach((p) => {
        porMesPessoa[p] = porMesPessoa[p] || {};
        porMesPessoa[p][mes] = (porMesPessoa[p][mes] || 0) + 1;
      });
    });
    const meses = Array.from(mesesSet).sort();
    const labels = meses.map((m) => {
      const [y, mo] = m.split("-");
      return `${mo}/${y}`;
    });
    const pessoas = Object.keys(porMesPessoa).sort();
    const datasets = pessoas.map((p, i) => ({
      label: p,
      data: meses.map((m) => porMesPessoa[p][m] || 0),
      borderColor: CORES[i % CORES.length],
      backgroundColor: CORES[i % CORES.length] + "33",
      tension: 0.3, fill: false,
    }));
    estado.graficos.evolucao = new Chart($("#grafico-evolucao"), {
      type: "line",
      data: { labels, datasets },
      options: baseOpts(true, "evolucao"),
    });
  }

  function contar(tarefas, campo) {
    const out = {};
    tarefas.forEach((t) => {
      const v = t[campo] || "—";
      out[v] = (out[v] || 0) + 1;
    });
    return out;
  }
  function baseOpts(horizontal, id) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" } },
      scales: id === "evolucao" ? {} : { x: { stacked: false }, y: { stacked: false, beginAtZero: true } },
    };
  }

  // ============================================================ TABELA
  function configurarOrdenacao() {
    $$("#tabela-tarefas th[data-ordem]").forEach((th) => {
      th.addEventListener("click", () => {
        const col = th.dataset.ordem;
        if (estado.ordenacao.coluna === col) {
          estado.ordenacao.dir = estado.ordenacao.dir === "asc" ? "desc" : "asc";
        } else {
          estado.ordenacao.coluna = col;
          estado.ordenacao.dir = "asc";
        }
        renderTabela();
      });
    });
  }

  function renderTabela(filtradas) {
    if (filtradas === undefined) filtradas = tarefasFiltradas();
    const tbody = $("#tabela-tarefas tbody");
    let lista = filtradas;

    // Busca textual.
    if (estado.busca) {
      lista = lista.filter((t) =>
        [t.nome, t.categoria, t.status, t.rotulos, (t.responsaveis || []).join(" "), t.notas]
          .filter(Boolean).join(" ").toLowerCase().includes(estado.busca)
      );
    }

    // Ordenação.
    const { coluna, dir } = estado.ordenacao;
    const mul = dir === "asc" ? 1 : -1;
    lista = [...lista].sort((a, b) => {
      let va = a[coluna], vb = b[coluna];
      if (coluna === "responsaveis") { va = (va || []).join("; "); vb = (vb || []).join("; "); }
      if (va == null) va = "";
      if (vb == null) vb = "";
      return String(va).localeCompare(String(vb), "pt-BR") * mul;
    });

    if (lista.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--cor-texto-suave);padding:2rem;">Nenhuma tarefa corresponde aos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = lista.map((t) => `
      <tr>
        <td><strong>${escapar(t.nome)}</strong>${t.notas ? `<br><span class="muted small">${escapar(t.notas.slice(0, 120))}${t.notas.length > 120 ? "…" : ""}</span>` : ""}</td>
        <td>${(t.responsaveis || []).map(escapar).join(", ") || "—"}</td>
        <td><span class="tag-status ${classeStatus(t.status)}">${escapar(t.status || "—")}</span></td>
        <td>${escapar(t.categoria || "—")}</td>
        <td>${fmtData(t.data_conclusao)}</td>
        <td>${escapar(t.rotulos || "—")}</td>
      </tr>
    `).join("");
  }

  // ============================================================ RESUMO POR IA
  async function gerarResumo() {
    const btn = $("#btn-gerar-resumo");
    const tamanho = $("#resumo-tamanho").value;
    const tom = $("#resumo-tom").value;
    const pessoa = $("#resumo-pessoa").value;

    esconder($("#resumo-erro")); esconder($("#resumo-saida"));
    mostrar($("#resumo-status"));
    $("#resumo-status-texto").textContent = "Gerando resumo… (pode levar alguns segundos)";
    btn.disabled = true;

    // Coleta os filtros ativos no formato esperado pelo backend.
    const filtros = {
      pessoas: Array.from(estado.filtros.pessoas),
      categorias: Array.from(estado.filtros.categorias),
      status: Array.from(estado.filtros.status),
      rotulos: Array.from(estado.filtros.rotulos),
      data_conclusao: specPeriodoParaAPI("conclusao", "data_conclusao"),
    };

    try {
      const resp = await fetch("/api/resumo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filtros, tamanho, tom, pessoa }),
      });
      const payload = await resp.json();
      esconder($("#resumo-status"));
      btn.disabled = false;

      if (!payload.ok) {
        $("#resumo-erro").textContent = payload.erro || "Erro ao gerar resumo.";
        mostrar($("#resumo-erro"));
        return;
      }
      const r = payload.resultado;
      if (r.erro || !r.texto) {
        $("#resumo-erro").textContent = r.erro || "Não foi possível gerar o resumo.";
        mostrar($("#resumo-erro"));
        return;
      }
      const saida = $("#resumo-saida");
      $(".resumo-meta", saida).innerHTML =
        `Modelo: <strong>${escapar(r.provedor)} / ${escapar(r.modelo)}</strong> • ` +
        `${r.tarefas_consideradas} tarefa(s) considerada(s) • ${payload.n_tarefas_filtradas} filtrada(s)`;
      $(".resumo-texto", saida).textContent = r.texto;
      mostrar(saida);
      mostrar($("#btn-copiar-resumo"));
    } catch (err) {
      esconder($("#resumo-status"));
      btn.disabled = false;
      $("#resumo-erro").textContent = "Erro de comunicação: " + err.message;
      mostrar($("#resumo-erro"));
    }
  }

  function specPeriodoParaAPI(prefixo, chave) {
    const f = estado.filtros[chave];
    if (!f) return { tipo: "preset", preset: "tudo" };
    if (f.tipo === "range") {
      // input[type=date] já retorna ISO (YYYY-MM-DD).
      return {
        tipo: "range",
        inicio: $(`#${prefixo}-ini`).value || null,
        fim: $(`#${prefixo}-fim`).value || null,
      };
    }
    return { tipo: "preset", preset: f.preset };
  }

  async function copiarResumo() {
    const texto = $(".resumo-texto").textContent;
    try {
      await navigator.clipboard.writeText(texto);
      const btn = $("#btn-copiar-resumo");
      const antigo = btn.textContent;
      btn.textContent = "Copiado!";
      setTimeout(() => { btn.textContent = antigo; }, 1500);
    } catch (e) {
      alert("Não foi possível copiar automaticamente. Selecione o texto manualmente.");
    }
  }

  // ============================================================ INIT
  document.addEventListener("DOMContentLoaded", configurarUpload);
})();
