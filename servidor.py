# -*- coding: utf-8 -*-
"""
Painel KAT — servidor para HOSPEDAGEM (Render, Railway, etc.), com IA nativa.

Experiência de quem visualiza: abre o link e usa. NÃO digita senha nenhuma.

Como a chave fica protegida sem pedir senha:
  - A chave da Anthropic fica SOMENTE na variável de ambiente do host
    (ANTHROPIC_API_KEY) — nunca vai para o navegador nem para o repositório.
  - Acesso por TOKEN no link (opcional, recomendado): você compartilha
    https://SEU-APP/?t=SEU_TOKEN — a pessoa só clica; o servidor grava um cookie
    e ela nunca mais vê token nem senha. Sem o token, mostra "acesso restrito".
    Se você não definir ACCESS_TOKEN, o painel fica totalmente aberto.
  - Limite de requisições por IP (RATE_MAX/min) para evitar abuso.
  - (Fora daqui) coloque um LIMITE DE GASTO na chave, no console da Anthropic.

Variáveis de ambiente no host:
  ANTHROPIC_API_KEY  -> sua chave (nova, rotacionada)      [obrigatória p/ IA]
  ACCESS_TOKEN       -> um segredo p/ o link (ex.: kat-demo-9x2)  [opcional]
  MODELO             -> modelo da IA (padrão claude-haiku-4-5, mais barato;
                        pode trocar p/ claude-sonnet-5 ou claude-opus-4-8)  [opcional]
  RATE_MAX           -> requisições de IA por minuto por IP (padrão 30)
  PORT               -> definido automaticamente pelo host
"""
import base64, json, os, time, threading, collections
from urllib.parse import urlparse, parse_qs
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
CANDIDATOS_BASE = [
    os.path.join(AQUI, "crm_kat_base_ficticia.xlsx"),
    os.path.join(os.path.dirname(AQUI), "dados", "crm_kat_base_ficticia.xlsx"),
]
BASE = next((p for p in CANDIDATOS_BASE if os.path.exists(p)), CANDIDATOS_BASE[0])
PORT = int(os.environ.get("PORT", os.environ.get("PORTA", "7799")))
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
RATE_MAX = int(os.environ.get("RATE_MAX", "30"))
# Modelo padrão: Haiku (mais barato). Trocável pela variável de ambiente MODELO
# (ex.: claude-sonnet-5 ou claude-opus-4-8) sem mexer no código.
MODELO = os.environ.get("MODELO", "claude-haiku-4-5")

try:
    import anthropic
except Exception:
    anthropic = None

# ---------------------------------------------------------------------------
# Contexto por visão (a partir da base fictícia)
# ---------------------------------------------------------------------------
import re, unicodedata
xl = pd.read_excel(BASE, sheet_name=None)
cli = xl["clientes"]; ass = xl["assessores"]; reun = xl["reunioes"]
inv = xl["investimentos"]; ser = xl["ecossistema_servicos"]
afi = xl.get("afi_planejamento", cli.iloc[0:0]); ativ = xl.get("atividades", cli.iloc[0:0])
aten = xl.get("atendimentos", cli.iloc[0:0])

sp = ass[(ass["unidade"] == "São Paulo") & (ass["cargo"].str.contains("Assessor"))]
cont = cli.groupby("assessor_id").size()
sp = sp.assign(n=sp["assessor_id"].map(cont).fillna(0)).sort_values("n")
ASSESSOR_ID = sp.iloc[len(sp)//2]["assessor_id"]
ASSESSOR_NOME = sp.iloc[len(sp)//2]["assessor"]
UNIDADE = "São Paulo"
ult_reuniao = reun.groupby("cliente_id")["data"].max()

def brl(v):
    v = float(v)
    if v >= 1e6: return f"R$ {v/1e6:.1f} mi"
    if v >= 1e3: return f"R$ {v/1e3:.0f} mil"
    return f"R$ {v:.0f}"

# --- Vencimentos (data-base da base fictícia = 15/07/2026) ---
HOJE = pd.Timestamp("2026-07-15")
inv["_venc"] = pd.to_datetime(inv["vencimento"], errors="coerce")
nome_por_id = dict(zip(cli["cliente_id"], cli["nome"]))

def proximos_vencimentos(ids, dias=60, cap=30):
    v = inv[inv["cliente_id"].isin(ids) & inv["_venc"].notna()]
    v = v[(v["_venc"] >= HOJE) & (v["_venc"] <= HOJE + pd.Timedelta(days=dias))].sort_values("_venc")
    if v.empty:
        return f"Nenhum vencimento nos próximos {dias} dias."
    linhas = []
    for _, x in v.head(cap).iterrows():
        nm = nome_por_id.get(x["cliente_id"], str(x["cliente_id"]))
        ur = ult_reuniao.get(x["cliente_id"])
        if pd.notna(ur):
            ur_dt = pd.to_datetime(ur)
            dias_contato = (HOJE - ur_dt).days
            contato = f"último contato {ur_dt.strftime('%d/%m/%Y')} (há {dias_contato} dias)"
        else:
            contato = "sem contato registrado"
        dias_venc = (x["_venc"] - HOJE).days
        linhas.append(f'- vence {x["_venc"].strftime("%d/%m/%Y")} (em {dias_venc}d) · {nm} · '
                      f'{x["ativo"]} ({x["indexador"]}) · {brl(x["valor_atual"])} · {contato}')
    extra = len(v) - cap
    if extra > 0:
        linhas.append(f"(+{extra} outros vencimentos no período)")
    return "\n".join(linhas)

def ecossistema_resumo(ids, top=None):
    s = ser[ser["cliente_id"].isin(ids) & (ser["status"] == "Ativo")]
    if s.empty:
        return "Nenhum serviço de ecossistema ativo no escopo."
    por_cat = s.groupby("categoria").agg(n=("cliente_id", "count"), rec=("receita_gerada_12m", "sum")).sort_values("rec", ascending=False)
    por_inst = s.groupby("instituicao").size().sort_values(ascending=False)
    L = [f"Total de serviços ativos: {len(s)}.", "Por categoria (ativos · receita 12m):"]
    for cat, r in por_cat.iterrows():
        L.append(f"- {cat}: {int(r.n)} · {brl(r.rec)}")
    seg = s[s["categoria"].str.startswith("Seguro")]
    if len(seg):
        L.append("Seguros por seguradora (ativos): " + ", ".join(f"{i} {int(n)}" for i, n in seg.groupby("instituicao").size().sort_values(ascending=False).items()))
    L.append("Por instituição (todos os contratos ativos):")
    items = list(por_inst.items())
    if top:
        items = items[:top]
    for inst, n in items:
        L.append(f"- {inst}: {int(n)}")
    if top and len(por_inst) > top:
        L.append(f"(+{len(por_inst)-top} outras instituições)")
    return "\n".join(L)

# mapas de assessor -> nome/unidade (para troca de usuário na visão Assessor)
nome_assessor = dict(zip(ass["assessor_id"], ass["assessor"]))
uni_assessor = dict(zip(ass["assessor_id"], ass["unidade"]))
UNIDADES = list(dict.fromkeys(cli["unidade"]))

def contexto_carteira(ids, limite=40):
    c = cli[cli["cliente_id"].isin(ids)].sort_values("patrimonio_investimentos_safra", ascending=False)
    out = []
    for _, r in c.head(limite).iterrows():
        ur = ult_reuniao.get(r["cliente_id"])
        ur = pd.to_datetime(ur).strftime("%d/%m/%Y") if pd.notna(ur) else "sem registro"
        out.append(f'- {r["nome"]} ({r["tipo"]}, {r["perfil_investidor"]}) | AUC {brl(r["patrimonio_investimentos_safra"])} | '
                   f'ecossistema {r["pct_adesao_ecossistema"]}% ({"apto" if r["ecossistema_apto"]=="Sim" else "não apto"}) | '
                   f'{r["num_classes_investimentos"]}/6 classes | AFI {r["tem_afi"]} | última reunião {ur} | '
                   f'receita 12m {brl(r["receita_total_12m"])}')
    return "\n".join(out)

def resumo(ids, titulo):
    c = cli[cli["cliente_id"].isin(ids)]
    return (f"{titulo}\nClientes: {len(c)} | PL/AUC total: {brl(c['patrimonio_investimentos_safra'].sum())} | "
        f"ecossistema médio: {c['pct_adesao_ecossistema'].mean():.0f}% | aptos: {(c['ecossistema_apto']=='Sim').sum()}\n"
        f"Receita 12m por segmento — Investimentos {brl(c['receita_investimentos_12m'].sum())}, "
        f"Seguros {brl(c['receita_seguros_12m'].sum())}, Consórcio {brl(c['receita_consorcio_12m'].sum())}, "
        f"Câmbio {brl(c['receita_cambio_12m'].sum())}, Crédito {brl(c['receita_credito_12m'].sum())}\n")

ids_ass = set(cli[cli["assessor_id"] == ASSESSOR_ID]["cliente_id"])
ids_uni = set(cli[cli["unidade"] == UNIDADE]["cliente_id"])
ids_all = set(cli["cliente_id"])

def por_assessor(ids):
    g = cli[cli["cliente_id"].isin(ids)].groupby("assessor").agg(
        clientes=("cliente_id","count"), AUC=("patrimonio_investimentos_safra","sum"),
        receita=("receita_total_12m","sum"), aptos=("ecossistema_apto", lambda s:(s=="Sim").sum()))
    return "\n".join(f'- {a}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}, aptos {int(r.aptos)}'
                     for a, r in g.sort_values("receita", ascending=False).iterrows())

def por_unidade():
    g = cli.groupby("unidade").agg(clientes=("cliente_id","count"), AUC=("patrimonio_investimentos_safra","sum"),
        receita=("receita_total_12m","sum"), eco=("pct_adesao_ecossistema","mean"),
        aptos=("ecossistema_apto", lambda s:(s=="Sim").sum()))
    return "\n".join(f'- {u}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}, '
                     f'ecossistema {r.eco:.0f}%, aptos {int(r.aptos)}'
                     for u, r in g.sort_values("receita", ascending=False).iterrows())

def assessores_por_unidade():
    g = cli.groupby(["unidade", "assessor"]).agg(
        clientes=("cliente_id", "count"), AUC=("patrimonio_investimentos_safra", "sum"),
        receita=("receita_total_12m", "sum"))
    out = []
    for uni in UNIDADES:
        if uni not in g.index.get_level_values(0):
            continue
        out.append(f"{uni}:")
        for (_, a), r in g.loc[[uni]].sort_values("receita", ascending=False).iterrows():
            out.append(f"  - {a}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}")
    return "\n".join(out)

def receita_por_unidade():
    g = cli.groupby("unidade").agg(
        inv=("receita_investimentos_12m", "sum"), seg=("receita_seguros_12m", "sum"),
        con=("receita_consorcio_12m", "sum"), cam=("receita_cambio_12m", "sum"),
        cred=("receita_credito_12m", "sum")).sort_values("seg", ascending=False)
    return "\n".join(f"- {u}: Seguros {brl(r.seg)}, Consórcio {brl(r.con)}, Câmbio {brl(r.cam)}, "
                     f"Crédito {brl(r.cred)}, Investimentos {brl(r.inv)}" for u, r in g.iterrows())

def ids_de(scope, ent=None):
    if scope == "assessor" and ent in nome_assessor:
        return set(cli[cli["assessor_id"] == ent]["cliente_id"])
    if scope == "lider" and ent in set(cli["unidade"]):
        return set(cli[cli["unidade"] == ent]["cliente_id"])
    # 'investimentos' (mesa de produtos) enxerga a base toda, como Gestão
    return {"assessor": ids_ass, "lider": ids_uni}.get(scope, ids_all)

_ctx_cache = {}
def contexto_de(scope, ent=None):
    key = (scope, ent)
    if key in _ctx_cache:
        return _ctx_cache[key]
    if scope == "assessor":
        ids = ids_de("assessor", ent)
        nome = nome_assessor.get(ent, ASSESSOR_NOME)
        uni = uni_assessor.get(ent, UNIDADE)
        ctx = (resumo(ids, f"VISÃO ASSESSOR — {nome} (Unidade {uni})") + "\nCLIENTES DA CARTEIRA:\n" + contexto_carteira(ids, 40)
            + "\n\nPRÓXIMOS VENCIMENTOS (60 dias, data-base 15/07/2026):\n" + proximos_vencimentos(ids, 60)
            + "\n\nECOSSISTEMA — SERVIÇOS ATIVOS:\n" + ecossistema_resumo(ids))
    elif scope == "lider":
        uni = ent if ent in set(cli["unidade"]) else UNIDADE
        ids = set(cli[cli["unidade"] == uni]["cliente_id"])
        ctx = (resumo(ids, f"VISÃO TEAM LEADER — Unidade {uni}") + "\nPOR ASSESSOR:\n" + por_assessor(ids) + "\n\nMAIORES CLIENTES:\n" + contexto_carteira(ids, 20)
            + "\n\nPRÓXIMOS VENCIMENTOS (60 dias, data-base 15/07/2026):\n" + proximos_vencimentos(ids, 60)
            + "\n\nECOSSISTEMA — SERVIÇOS ATIVOS:\n" + ecossistema_resumo(ids, top=20))
    else:
        ctx = (resumo(ids_all, "VISÃO GESTÃO — KAT (5 unidades, 20 assessores)") + "\nPOR UNIDADE:\n" + por_unidade()
            + "\n\nRECEITA POR UNIDADE E SEGMENTO (12m):\n" + receita_por_unidade()
            + "\n\nASSESSORES POR UNIDADE:\n" + assessores_por_unidade()
            + "\n\nPRÓXIMOS VENCIMENTOS (45 dias, data-base 15/07/2026):\n" + proximos_vencimentos(ids_all, 45)
            + "\n\nECOSSISTEMA — SERVIÇOS ATIVOS:\n" + ecossistema_resumo(ids_all, top=20))
    _ctx_cache[key] = ctx
    return ctx

_nomes_lower = [(str(r["nome"]).lower(), r["cliente_id"]) for _, r in cli.iterrows()]

def detalhe_cliente(cid):
    r = cli[cli["cliente_id"] == cid]
    if r.empty:
        return ""
    r = r.iloc[0]
    L = [f"\n=== DETALHE DO CLIENTE {r['nome']} ({cid}) ==="]
    L.append(f"Tipo {r['tipo']} · perfil {r['perfil_investidor']} · assessor {r['assessor']} · unidade {r['unidade']} · "
             f"cliente desde {pd.to_datetime(r['cliente_desde']).strftime('%d/%m/%Y')}")
    L.append(f"AUC {brl(r['patrimonio_investimentos_safra'])} · ecossistema {r['pct_adesao_ecossistema']}% "
             f"({'apto' if r['ecossistema_apto']=='Sim' else 'não apto'}) · AFI {r['tem_afi']} · receita 12m {brl(r['receita_total_12m'])}")
    h = inv[inv["cliente_id"] == cid]
    if len(h):
        L.append("Carteira: " + "; ".join(f"{x['produto']} {x['ativo']} ({x['classe']}, {brl(x['valor_atual'])})" for _, x in h.head(12).iterrows()))
    s = ser[(ser["cliente_id"] == cid) & (ser["status"] == "Ativo")]
    if len(s):
        L.append("Ecossistema ativo: " + "; ".join(f"{x['categoria']} — {x['instituicao']}" for _, x in s.iterrows()))
    a = afi[afi["cliente_id"] == cid]
    if len(a):
        a = a.iloc[0]
        L.append(f"AFI: renda {brl(a['renda_mensal'])}, {a['composicao_familiar']}; objetivos: {a['objetivos']}; projetos: {a['projetos']}")
    rr = reun[reun["cliente_id"] == cid].sort_values("data", ascending=False)
    if len(rr):
        L.append("Reuniões (mais recentes primeiro):")
        for _, x in rr.head(5).iterrows():
            L.append(f"- {pd.to_datetime(x['data']).strftime('%d/%m/%Y')} [{x['tipo']}]: {x['resumo_transcricao']} "
                     f"(insight AFI: {x['insight_afi']}; próxima ação: {x['proxima_acao']})")
    else:
        L.append("Reuniões: nenhuma registrada.")
    if len(ativ):
        at = ativ[ativ["cliente_id"] == cid]
        if len(at):
            L.append("Atividades da mesa: " + "; ".join(f"{x['produto']} ({x['status']})" for _, x in at.head(6).iterrows()))
    return "\n".join(L)

# ---------------------------------------------------------------------------
# FERRAMENTAS — a IA consulta a base sozinha (tool use)
# ---------------------------------------------------------------------------
def _dims(df):
    """Anexa nome/assessor/unidade do cliente, para permitir filtrar e agrupar por eles."""
    if "cliente_id" not in df.columns:
        return df
    faltando = [c for c in ("nome", "assessor", "unidade") if c not in df.columns]
    return df.merge(cli[["cliente_id"] + faltando], on="cliente_id", how="left") if faltando else df

TABELAS = {
    "clientes": cli, "investimentos": _dims(inv), "ecossistema_servicos": _dims(ser),
    "reunioes": _dims(reun), "atividades": _dims(ativ), "atendimentos": _dims(aten),
    "afi_planejamento": _dims(afi),
}
FUNCS = {"soma": "sum", "media": "mean", "contagem": "count", "min": "min", "max": "max", "contagem_unica": "nunique"}

def _num(serie, valor):
    if pd.api.types.is_datetime64_any_dtype(serie):
        return pd.to_datetime(valor, errors="coerce", dayfirst=True)
    if pd.api.types.is_numeric_dtype(serie):
        try: return float(valor)
        except Exception: return valor
    return valor

def ferramenta_consultar(ids, tabela=None, filtros=None, agrupar_por=None, metricas=None,
                         ordenar_por=None, ordem="desc", limite=25, colunas=None):
    df = TABELAS.get(tabela)
    if df is None:
        return f"Tabela '{tabela}' não existe. Disponíveis: {', '.join(TABELAS)}."
    df = df[df["cliente_id"].isin(ids)]
    for f in (filtros or []):
        col, op, val = f.get("coluna"), f.get("operador", "="), f.get("valor")
        if col not in df.columns:
            return f"Coluna '{col}' não existe em {tabela}. Colunas: {', '.join(df.columns)}."
        s = df[col]
        if op == "contem":
            df = df[s.astype(str).str.contains(str(val), case=False, na=False)]
        elif op == "em":
            alvos = [v.strip().lower() for v in str(val).split(",")]
            df = df[s.astype(str).str.lower().isin(alvos)]
        elif op in ("=", "!="):
            if s.dtype == object:
                igual = s.astype(str).str.lower() == str(val).lower()
            else:
                igual = s == _num(s, val)
            df = df[igual if op == "=" else ~igual]
        elif op in (">", ">=", "<", "<="):
            v = _num(s, val)
            df = df[{">" : s > v, ">=": s >= v, "<": s < v, "<=": s <= v}[op]]
        else:
            return f"Operador '{op}' inválido."
    if df.empty:
        return "Nenhum registro encontrado com esses filtros."
    limite = max(1, min(int(limite or 25), 60))
    if metricas:
        aggs = {}
        for m in metricas:
            c, fn = m.get("coluna"), FUNCS.get(m.get("funcao", "soma"))
            if c not in df.columns: return f"Coluna '{c}' não existe em {tabela}."
            if not fn: return f"Função '{m.get('funcao')}' inválida. Use: {', '.join(FUNCS)}."
            aggs[f"{m.get('funcao')}_{c}"] = (c, fn)
        if agrupar_por:
            faltam = [c for c in agrupar_por if c not in df.columns]
            if faltam: return f"Coluna(s) {faltam} não existem em {tabela}."
            g = df.groupby(agrupar_por).agg(**aggs)
        else:
            g = pd.DataFrame([{k: getattr(df[c], fn)() for k, (c, fn) in aggs.items()}])
        alvo = ordenar_por if ordenar_por in g.columns else (g.columns[0] if len(g.columns) else None)
        if alvo is not None:
            g = g.sort_values(alvo, ascending=(ordem == "asc"))
        total = len(g)
        txt = g.head(limite).round(2).to_string()
        if total > limite: txt += f"\n(+{total-limite} linhas omitidas de {total})"
        return txt[:6000]
    if ordenar_por and ordenar_por in df.columns:
        df = df.sort_values(ordenar_por, ascending=(ordem == "asc"))
    cols = [c for c in (colunas or []) if c in df.columns]
    if not cols:
        cols = list(dict.fromkeys([c for c in ("nome", "assessor", "unidade") if c in df.columns]
                                  + [c for c in df.columns if c != "cliente_id"]))[:8]
    total = len(df)
    txt = df[cols].head(limite).to_string(index=False)
    if total > limite: txt += f"\n(+{total-limite} linhas omitidas de {total})"
    return txt[:6000]

def _norm(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()

_nomes_norm = [(_norm(r["nome"]), r["cliente_id"]) for _, r in cli.iterrows()]

def ferramenta_detalhe_cliente(ids, termo=""):
    termo = str(termo).strip()
    if not termo:
        return "Informe o nome (mesmo parcial) ou o CPF/CNPJ do cliente."
    digitos = re.sub(r"\D", "", termo)
    if len(digitos) >= 11:
        achados = [c for c in cli["cliente_id"] if re.sub(r"\D", "", str(c)) == digitos]
    else:
        t = _norm(termo)
        achados = [c for n, c in _nomes_norm if t in n]
        if not achados:
            toks = [x for x in t.split() if len(x) >= 3]
            achados = [c for n, c in _nomes_norm if toks and all(tk in n for tk in toks)]
    achados = [c for c in achados if c in ids]
    if not achados:
        return (f"Nenhum cliente com '{termo}' no escopo desta visão. "
                "Ele pode existir na base mas pertencer a outro assessor/unidade.")
    if len(achados) > 1:
        lista = "; ".join(f'{r["nome"]} ({r["cliente_id"]})'
                          for _, r in cli[cli["cliente_id"].isin(achados)].head(12).iterrows())
        return (f"{len(achados)} clientes correspondem a '{termo}' neste escopo: {lista}. "
                "Pergunte ao usuário qual deles, ou chame de novo com o nome completo/CPF.")
    return detalhe_cliente(achados[0])[:6000]

FERRAMENTAS = [
    {
        "name": "consultar",
        "description": ("Consulta e agrega a base do CRM (só os clientes do escopo/visão atual). Use para QUALQUER "
            "pergunta quantitativa: totais, rankings, contagens, médias, filtros e agrupamentos — ex.: receita de "
            "seguros da Icatu por unidade, vencimentos dos próximos 7 dias, clientes sem reunião recente, ranking de "
            "assessores. Sempre consulte antes de dizer que não tem a informação."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tabela": {"type": "string", "enum": list(TABELAS),
                           "description": "Tabela a consultar."},
                "filtros": {"type": "array", "description": "Filtros combinados com E.", "items": {
                    "type": "object",
                    "properties": {
                        "coluna": {"type": "string"},
                        "operador": {"type": "string", "enum": ["=", "!=", ">", ">=", "<", "<=", "contem", "em"]},
                        "valor": {"type": "string", "description": "Valor. Datas em AAAA-MM-DD. 'em' aceita lista separada por vírgula."},
                    },
                    "required": ["coluna", "operador", "valor"]}},
                "agrupar_por": {"type": "array", "items": {"type": "string"},
                                "description": "Colunas para agrupar, ex.: ['unidade'] ou ['assessor']."},
                "metricas": {"type": "array", "description": "O que calcular. Sem métricas, devolve as linhas.", "items": {
                    "type": "object",
                    "properties": {
                        "coluna": {"type": "string"},
                        "funcao": {"type": "string", "enum": list(FUNCS)},
                    },
                    "required": ["coluna", "funcao"]}},
                "colunas": {"type": "array", "items": {"type": "string"}, "description": "Colunas a exibir quando não há métricas."},
                "ordenar_por": {"type": "string"},
                "ordem": {"type": "string", "enum": ["desc", "asc"]},
                "limite": {"type": "integer", "description": "Máx. de linhas (padrão 25, teto 60)."},
            },
            "required": ["tabela"],
        },
    },
    {
        "name": "detalhe_cliente",
        "description": ("Perfil completo de UM cliente: cadastro, carteira, serviços do ecossistema, AFI e o histórico de "
            "reuniões com o resumo/transcrição de cada uma. Use para perguntas sobre um cliente citado pelo nome (mesmo "
            "só o primeiro nome, ex.: 'beatriz') ou pelo CPF/CNPJ. Se houver mais de um, devolve a lista para escolher."),
        "input_schema": {
            "type": "object",
            "properties": {"termo": {"type": "string", "description": "Nome (completo ou parcial) ou CPF/CNPJ."}},
            "required": ["termo"],
        },
    },
]

def executar_ferramenta(nome, entrada, ids):
    if nome == "consultar":
        return ferramenta_consultar(ids, **(entrada or {}))
    if nome == "detalhe_cliente":
        return ferramenta_detalhe_cliente(ids, **(entrada or {}))
    return f"Ferramenta '{nome}' não existe."

ESQUEMA = """TABELAS (as ferramentas já filtram tudo para o escopo/visão atual — nunca vazam outros assessores):
- clientes: cliente_id, nome, tipo (PF/PJ), assessor, unidade, cliente_desde, perfil_investidor, patrimonio_investimentos_safra (=AUC), num_classes_investimentos, num_servicos_ecossistema, pct_adesao_ecossistema, ecossistema_apto (Sim/Não), tem_afi, receita_investimentos_12m, receita_seguros_12m, receita_consorcio_12m, receita_cambio_12m, receita_credito_12m, receita_total_12m
- investimentos (posições): cliente_id, nome, assessor, unidade, classe, produto, ativo, emissor_gestor, indexador, taxa, data_aplicacao, vencimento, valor_aplicado, valor_atual
- ecossistema_servicos: cliente_id, nome, assessor, unidade, categoria (Seguro de Vida | Seguro Saúde | Seguro Patrimonial | Consórcio | Câmbio | Financiamento Imóvel | Financiamento Veículo | Corporate), subtipo, instituicao (Icatu, Porto Seguro, SulAmérica, Prudential, MetLife, Tokio Marine, Bradesco Saúde, Embracon, Itaú, Santander, Inter, Safra, Oribank, Bradesco), status (Ativo | Negado), data_contratacao, valor_referencia, receita_gerada_12m
- reunioes: cliente_id, nome, assessor, unidade, data, tipo, canal, resumo_transcricao, insight_afi, proxima_acao
- atividades (mesa): cliente_id, nome, assessor, unidade, data, origem, produto, valor_envolvido, receita_estimada, status, motivo
- atendimentos: cliente_id, nome, assessor, unidade, data, area, solicitacao, prioridade, status, data_conclusao
- afi_planejamento: cliente_id, nome, assessor, unidade, renda_mensal, estado_civil, num_dependentes, composicao_familiar, patrimonio_imobiliario, objetivos, projetos, horizonte_anos, tolerancia_risco, aporte_mensal_planejado, reserva_emergencia_meses"""

SYSTEM = ("Você é o assistente de IA do CRM da KAT Investimentos (escritório de assessoria de investimentos). "
    "Responda em português (BR), objetivo, conciso e útil para um assessor/gestor.\n\n"
    "VOCÊ TEM ACESSO À BASE POR FERRAMENTAS — use-as:\n"
    "- 'consultar': qualquer pergunta quantitativa (totais, rankings, contagens, filtros, agrupamentos). "
    "Ex.: receita de seguros da Icatu por unidade = tabela ecossistema_servicos, filtros instituicao=Icatu e "
    "categoria contem Seguro e status=Ativo, agrupar_por ['unidade'], metricas soma de receita_gerada_12m.\n"
    "- 'detalhe_cliente': perguntas sobre um cliente específico (registro/resumo de reuniões, carteira, serviços, AFI). "
    "Aceita só o primeiro nome.\n"
    "REGRA IMPORTANTE: NUNCA diga que não tem a informação sem antes tentar a ferramenta. Só afirme que não há dado "
    "depois que a consulta voltar vazia. Pode encadear várias consultas. Não invente números: todo valor vem da ferramenta "
    "ou do resumo abaixo.\n"
    "A data de referência (hoje) é 15/07/2026. Valores em R$. Base fictícia de teste.\n\n"
    + ESQUEMA + "\n\n=== RESUMO DA VISÃO ATUAL ===\n")

def responder(scope, question, history, ent=None, area="geral"):
    if anthropic is None:
        return {"error": "Biblioteca 'anthropic' não instalada no servidor."}
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return {"error": "Servidor sem ANTHROPIC_API_KEY configurada."}
    if not str(question).strip():
        return {"error": "Pergunta vazia."}
    ids = ids_de(scope, ent)
    ctx = contexto_de(scope, ent)
    if area == "investimentos":
        ctx = ("[TELA INVESTIMENTOS/PRODUTOS] Foque em produtos de investimento: classes (Renda Fixa Pós/Pré/IPCA+, "
               "Renda Variável, Fundos, COE, Previdência — a classe 'Internacional' existe na base mas ficou fora desta tela), "
               "alocação e mix vs meta por segmento, oportunidades da mesa (tabela atividades: origem, status, receita_estimada, "
               "motivo) e vencimentos a reaplicar. Use a ferramenta 'consultar' nas tabelas investimentos e atividades.\n\n" + ctx)
    elif area == "tickets":
        ctx = ("[TELA TICKETS/ATENDIMENTOS] Foque nos atendimentos às áreas operacionais. Use a ferramenta 'consultar' na tabela "
               "'atendimentos': area (Operações RV, Operações RF, Abertura de Conta, Eventos, Reembolsos), solicitacao (tipo), "
               "prioridade, status (etapa: Solicitação/Em andamento/Pendência/Concluído), sla_horas (prazo-alvo), data (abertura). "
               "Um ticket está com SLA estourado se (data + sla_horas) já passou de 15/07/2026 14:00 e status != Concluído. "
               "Responda sobre backlog por área, pendências, tickets em risco/vencidos e concluídos.\n\n" + ctx)
    elif area == "comercial":
        ctx = ("[TELA COMERCIAL/PROSPECÇÃO] Foque no funil de prospecção (etapas Novo → R1 → R2 → Conta aberta), enriquecimento "
               "de prospects (CNPJs, atuação, região por DDD), tarefas/retornos e clientes similares da base para preparar reuniões. "
               "Para 'clientes similares', use 'consultar' na tabela clientes (perfil_investidor, patrimonio_investimentos_safra, "
               "num_classes_investimentos) filtrando pelo mesmo perfil.\n\n" + ctx)
    elif area == "academy":
        ctx = ("[KAT ACADEMY — IA TUTORA] Você é um tutor de ensino da KAT Academy. O objetivo é ENSINAR e aprofundar o "
               "aprendizado do assessor sobre assessoria de investimentos, os produtos do ecossistema (Renda Fixa, RV, Fundos, "
               "COE, Previdência, Seguros, Câmbio, Crédito, Consórcio), a régua de relacionamento 12+4+1, condução de reuniões "
               "(R1/R2), planejamento financeiro pessoal e reserva de emergência, soft skills e postura profissional. "
               "Responda de forma didática, estruturada e com exemplos práticos, como um professor. Pode usar analogias e passos. "
               "Não precisa consultar a base de clientes a menos que o aluno peça um exemplo com dados reais; foque no conteúdo "
               "educacional das trilhas.\n\n" + ctx)
    msgs = [{"role": h["role"], "content": str(h["content"])[:4000]}
            for h in (history or [])[-8:] if h.get("role") in ("user","assistant") and h.get("content")]
    msgs.append({"role": "user", "content": str(question)[:4000]})
    cliente_ia = anthropic.Anthropic()
    try:
        for _ in range(6):  # loop de tool use: consulta a base até ter a resposta
            resp = cliente_ia.messages.create(model=MODELO, max_tokens=1500,
                                              system=SYSTEM + ctx, tools=FERRAMENTAS, messages=msgs)
            if resp.stop_reason != "tool_use":
                txt = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
                return {"answer": txt.strip() or "(sem resposta)"}
            msgs.append({"role": "assistant", "content": resp.content})
            resultados = []
            for b in resp.content:
                if getattr(b, "type", "") != "tool_use":
                    continue
                try:
                    saida, erro = executar_ferramenta(b.name, b.input, ids), False
                except Exception as e:
                    saida, erro = f"Erro na consulta: {e}. Revise as colunas/filtros e tente de novo.", True
                resultados.append({"type": "tool_result", "tool_use_id": b.id,
                                   "content": str(saida)[:6000], "is_error": erro})
            msgs.append({"role": "user", "content": resultados})
        return {"answer": "Não consegui concluir a consulta (muitas etapas). Tente perguntar de forma mais específica."}
    except Exception as e:
        return {"error": f"Erro ao chamar a IA: {e}"}


# --- Enriquecimento de prospect via busca web (Claude + web_search) ---
DDD_REGIAO = {
    "11": "São Paulo/SP (capital e Grande SP)", "12": "Vale do Paraíba/SP", "13": "Baixada Santista/SP",
    "14": "Bauru/Marília/SP", "15": "Sorocaba/SP", "16": "Ribeirão Preto/SP", "17": "S. J. do Rio Preto/SP",
    "18": "Presidente Prudente/SP", "19": "Campinas/SP", "21": "Rio de Janeiro/RJ", "22": "Campos/RJ",
    "27": "Vitória/ES", "31": "Belo Horizonte/MG", "32": "Juiz de Fora/MG", "34": "Uberlândia/MG (Triângulo Mineiro)",
    "35": "Sul de Minas/MG", "37": "Divinópolis/MG", "38": "Montes Claros/MG", "41": "Curitiba/PR",
    "43": "Londrina/PR", "44": "Maringá/PR", "47": "Joinville/Blumenau/SC", "48": "Florianópolis/SC",
    "51": "Porto Alegre/RS", "54": "Caxias do Sul/RS", "61": "Brasília/DF", "62": "Goiânia/GO",
    "31": "Belo Horizonte/MG", "71": "Salvador/BA", "81": "Recife/PE", "85": "Fortaleza/CE", "92": "Manaus/AM",
}


def _ddd_de(fone):
    d = re.sub(r"\D", "", str(fone))
    m = re.search(r"(?:55)?0?(\d{2})9?\d{3,4}\d{4}", d)
    if m:
        return m.group(1)
    return d[:2] if len(d) >= 2 else ""


def enriquecer(nome, telefone, cpf=""):
    if anthropic is None:
        return {"error": "Biblioteca 'anthropic' não instalada no servidor."}
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return {"error": "Servidor sem ANTHROPIC_API_KEY — enriquecimento por IA indisponível."}
    nome = str(nome).strip()
    if len(nome.split()) < 2:
        return {"error": "Informe o nome completo do prospect."}
    ddd = _ddd_de(telefone)
    regiao_hint = DDD_REGIAO.get(ddd, "região não identificada pelo DDD")
    sistema = (
        "Você é um assistente de prospecção de um escritório de assessoria de investimentos. A partir do NOME COMPLETO e do "
        "TELEFONE (o DDD indica a região) de um possível cliente, pesquise na web informações PÚBLICAS para enriquecer o cadastro. "
        "Priorize: empresas/CNPJs em que a pessoa aparece como sócia/administradora (Receita Federal, portais de CNPJ, jucesp, etc.), "
        "setor de atuação, cargo e a cidade/região. USE O DDD para escolher a pessoa certa quando o nome for comum "
        "(ex.: DDD 34 = Triângulo Mineiro). Use apenas dados públicos e plausíveis; quando não tiver certeza, deixe o campo vazio e "
        "baixe a confiança — é melhor não afirmar do que errar a pessoa. Ao final, responda APENAS com um bloco JSON, sem nenhum texto "
        'fora dele, no formato exato: {"regiao":"", "profissao":"", "atuacao":"", "cargo":"", '
        '"cnpjs":[{"cnpj":"", "razao":"", "setor":""}], "resumo":"", "confianca":"alta|média|baixa"}')
    usuario = (f"Nome completo: {nome}\nTelefone: {telefone} (DDD {ddd} → {regiao_hint})"
               + (f"\nCPF: {cpf}" if str(cpf).strip() else "") + "\nPesquise online e devolva somente o JSON.")
    try:
        client = anthropic.Anthropic()
        msgs = [{"role": "user", "content": usuario}]
        resp = None
        for _ in range(4):
            resp = client.messages.create(
                model=MODELO, max_tokens=1800, system=sistema,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                messages=msgs)
            if resp.stop_reason == "pause_turn":
                msgs = [{"role": "user", "content": usuario}, {"role": "assistant", "content": resp.content}]
                continue
            break
        txt = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        enr = json.loads(m.group(0)) if m else {}
        if not isinstance(enr, dict):
            enr = {}
        enr.setdefault("regiao", regiao_hint)
        enr.setdefault("cnpjs", [])
        enr.setdefault("atuacao", "")
        enr["fonte"] = f"Busca web (Claude + web_search) · DDD {ddd}" + (f" · confiança {enr.get('confianca')}" if enr.get("confianca") else "")
        return {"enrich": enr}
    except Exception as e:
        return {"error": f"Não foi possível enriquecer agora: {e}"}

# --- limite de requisições por IP (proteção de custo, invisível p/ o usuário) ---
_hits = collections.defaultdict(list)
_lock = threading.Lock()
def rate_ok(ip):
    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.pop(0)
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True

PAGINA_NEGADO = ("<!doctype html><meta charset=utf-8><title>Acesso restrito</title>"
    "<div style='font-family:system-ui;max-width:460px;margin:16vh auto;text-align:center;color:#1f4a43'>"
    "<div style='font-size:26px;font-weight:800;letter-spacing:3px'>KAT <span style='color:#c4a461'>INVESTIMENTOS</span></div>"
    "<p style='margin-top:18px;color:#5b6b67'>Acesso restrito. Use o link de convite que você recebeu "
    "(ele contém o código de acesso).</p></div>")

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=AQUI, **k)

    def _ip(self):
        return (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def _acesso(self):
        """'ok' = liberado; 'set' = token válido no link (gravar cookie); 'no' = negado."""
        if not ACCESS_TOKEN:
            return "ok"
        if f"kat_sess={ACCESS_TOKEN}" in self.headers.get("Cookie", ""):
            return "ok"
        if parse_qs(urlparse(self.path).query).get("t", [""])[0] == ACCESS_TOKEN:
            return "set"
        return "no"

    def _negar_html(self):
        body = PAGINA_NEGADO.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        acc = self._acesso()
        if acc == "no":
            return self._negar_html()
        if acc == "set":
            # grava cookie e redireciona para a URL limpa (some o token da barra)
            self.send_response(302)
            self.send_header("Set-Cookie", f"kat_sess={ACCESS_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path in ("/", ""):
            self.path = "/painel_kat.html"
        return super().do_GET()

    def do_POST(self):
        if self._acesso() == "no":
            return self._json({"error": "Acesso não autorizado."}, 403)
        if self.path.split("?")[0] == "/api/chat":
            if not rate_ok(self._ip()):
                return self._json({"error": "Muitas perguntas em pouco tempo. Aguarde alguns segundos."}, 429)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            return self._json(responder(data.get("scope", "assessor"), data.get("question", ""), data.get("history"), data.get("ent"), data.get("area", "geral")))
        if self.path.split("?")[0] == "/api/enriquecer":
            if not rate_ok(self._ip()):
                return self._json({"error": "Muitas requisições. Aguarde alguns segundos."}, 429)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            return self._json(enriquecer(data.get("nome", ""), data.get("telefone", ""), data.get("cpf", "")))
        self.send_error(404, "Not found")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    tem_chave = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    print(f"Painel KAT em http://{HOST}:{PORT} | acesso: {'token no link' if ACCESS_TOKEN else 'ABERTO'} "
          f"| limite IA: {RATE_MAX}/min por IP | IA: {'ativa' if tem_chave else 'sem chave'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
